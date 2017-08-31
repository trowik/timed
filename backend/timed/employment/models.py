"""Models for the employment app."""

from datetime import date, timedelta

from dateutil import rrule
from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import functions

from timed.models import WeekdaysField


class Location(models.Model):
    """Location model.

    A location is the place where an employee works.
    """

    name     = models.CharField(max_length=50, unique=True)
    workdays = WeekdaysField(default=[str(day) for day in range(1, 6)])
    """
    Workdays defined per location, default is Monday - Friday
    """

    def __str__(self):
        """Represent the model as a string.

        :return: The string representation
        :rtype:  str
        """
        return self.name


class PublicHoliday(models.Model):
    """Public holiday model.

    A public holiday is a day on which no employee of a certain location has
    to work.
    """

    name     = models.CharField(max_length=50)
    date     = models.DateField()
    location = models.ForeignKey(Location,
                                 related_name='public_holidays')

    def __str__(self):
        """Represent the model as a string.

        :return: The string representation
        :rtype:  str
        """
        return '{0} {1}'.format(self.name, self.date.strftime('%Y'))

    class Meta:
        """Meta information for the public holiday model."""

        indexes = [models.Index(fields=['date'])]


class AbsenceType(models.Model):
    """Absence type model.

    An absence type defines the type of an absence. E.g sickness, holiday or
    school.
    """

    name          = models.CharField(max_length=50)
    fill_worktime = models.BooleanField(default=False)

    def __str__(self):
        """Represent the model as a string.

        :return: The string representation
        :rtype:  str
        """
        return self.name


class AbsenceCredit(models.Model):
    """Absence credit model.

    An absence credit is a credit for an absence of a certain type. A user
    should only be able to create as many absences as defined in this credit.
    E.g a credit that defines that a user can only have 25 holidays.
    """

    user         = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     related_name='absence_credits')
    comment      = models.CharField(max_length=255, blank=True)
    absence_type = models.ForeignKey(AbsenceType,
                                     related_name='absence_credits')
    date         = models.DateField()
    days         = models.PositiveIntegerField(default=0)


class OvertimeCredit(models.Model):
    """Overtime credit model.

    An overtime credit is a transferred overtime from the last year. This is
    added to the worktime of a user.
    """

    user     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                 related_name='overtime_credits')
    date     = models.DateField()
    duration = models.DurationField(blank=True, null=True)


class UserAbsenceTypeManager(models.Manager):
    def with_user(self, user, start_date, end_date):
        """Get all user absence types with the needed calculations.

        This is achieved using a raw query because the calculations were too
        complicated to do with django annotations / aggregations. Since those
        proxy models are read only and don't need to be filtered or anything,
        the raw query shouldn't block any needed functions.

        :param User user: The user of the user absence type
        :param datetime.date start_date: Start date of the user absence type
        :param datetime.date end_date: End date of the user absence type
        :returns: User absence types for the requested user
        :rtype: django.db.models.QuerySet
        """
        from timed.tracking.models import Absence

        return UserAbsenceType.objects.raw("""
            SELECT
                at.*,
                %(user_id)s AS user_id,
                %(start)s AS start_date,
                %(end)s AS end_date,
                CASE
                    WHEN at.fill_worktime THEN NULL
                    ELSE credit_join.credit
                END AS credit,
                CASE
                    WHEN at.fill_worktime THEN NULL
                    ELSE used_join.used_days
                END AS used_days,
                CASE
                    WHEN at.fill_worktime THEN used_join.used_duration
                    ELSE NULL
                END AS used_duration,
                CASE
                    WHEN at.fill_worktime THEN NULL
                    ELSE credit_join.credit - used_join.used_days
                END AS balance
            FROM {absencetype_table} AS at
            LEFT JOIN (
                SELECT
                    at.id,
                    SUM(ac.days) AS credit
                FROM {absencetype_table} AS at
                LEFT JOIN {absencecredit_table} AS ac ON (
                    ac.absence_type_id = at.id
                    AND
                    ac.user_id = %(user_id)s
                    AND
                    ac.date BETWEEN %(start)s AND %(end)s
                )
                GROUP BY at.id, ac.absence_type_id
            ) AS credit_join ON (at.id = credit_join.id)
            LEFT JOIN (
                SELECT
                    at.id,
                    COUNT(a.id) AS used_days,
                    SUM(a.duration) AS used_duration
                FROM {absencetype_table} AS at
                LEFT JOIN {absence_table} AS a ON (
                    a.type_id = at.id
                    and
                    a.user_id = %(user_id)s
                    AND
                    a.date BETWEEN %(start)s AND %(end)s
                )
                GROUP BY at.id, a.type_id
            ) AS used_join ON (at.id = used_join.id)
        """.format(
            absence_table=Absence._meta.db_table,
            absencetype_table=AbsenceType._meta.db_table,
            absencecredit_table=AbsenceCredit._meta.db_table
        ), {
            'user_id': user.id,
            'start': start_date,
            'end': end_date
        })


class UserAbsenceType(AbsenceType):
    """User absence type.

    This is a proxy for the absence type model used to generate a fake relation
    between a user and an absence type. This is required so we can expose the
    absence credits in a clean way to the API.

    The PK of this model is a combination of the user ID and the actual absence
    type ID.
    """

    objects = UserAbsenceTypeManager()

    @property
    def pk(self):
        return '{0}-{1}'.format(self.user_id, self.id)

    class Meta:
        proxy = True


class EmploymentManager(models.Manager):
    """Custom manager for employments."""

    def get_at(self, user, date):
        """Get employment of user at given date.

        :param User user: The user of the searched employments
        :param datetime.date date: date of employment
        :returns: Employment
        """
        return self.get(
            (
                models.Q(end_date__gte=date) |
                models.Q(end_date__isnull=True)
            ),
            start_date__lte=date,
            user=user
        )

    def for_user(self, user, start, end):
        """Get employments in given time frame for current user.

        This includes overlapping employments.

        :param User user: The user of the searched employments
        :param datetime.date start: start of time frame
        :param datetime.date end: end of time frame
        :returns: queryset of employments
        """
        # end date NULL on database is like employment is ending today
        queryset = self.annotate(
            end=functions.Coalesce('end_date', models.Value(date.today()))
        )
        return queryset.filter(
            user=user
        ).exclude(
            models.Q(end__lt=start) | models.Q(start_date__gt=end)
        )


class Employment(models.Model):
    """Employment model.

    An employment represents a contract which defines where an employee works
    and from when to when.
    """

    user             = models.ForeignKey(settings.AUTH_USER_MODEL,
                                         related_name='employments')
    location         = models.ForeignKey(Location, related_name='employments')
    percentage       = models.IntegerField(validators=[
                                           MinValueValidator(0),
                                           MaxValueValidator(100)])
    worktime_per_day = models.DurationField()
    start_date       = models.DateField()
    end_date         = models.DateField(blank=True, null=True)
    objects          = EmploymentManager()

    def __str__(self):
        """Represent the model as a string.

        :return: The string representation
        :rtype:  str
        """
        return '{0} ({1} - {2})'.format(
            self.user.username,
            self.start_date.strftime('%d.%m.%Y'),
            self.end_date.strftime('%d.%m.%Y') if self.end_date else 'today'
        )

    def calculate_worktime(self, start, end):
        """Calculate reported, expected and balance for employment.

        1. It shortens the time frame so it is within given employment
        1. Determine the count of workdays within time frame
        2. Determine the count of public holidays within time frame
        3. The expected worktime consists of following elements:
            * Workdays
            * Subtracted by holidays
            * Multiplicated with the worktime per day of the employment
        4. Determine the overtime credit duration within time frame
        5. The reported worktime is the sum of the durations of all reports
           for this user within time frame
        6. The absences are all absences for this user within time frame
        7. The balance is the reported time plus the absences plus the
            overtime credit minus the expected worktime

        :param start: calculate worktime starting on given day.
        :param end:   calculate worktime till given day
        :returns:     tuple of 3 values reported, expected and balance in given
                      time frame
        """
        from timed.tracking.models import Absence, Report

        # shorten time frame to employment
        start = max(start, self.start_date)
        end = min(self.end_date or date.today(), end)

        # workdays is in isoweekday, byweekday expects Monday to be zero
        week_workdays = [int(day) - 1 for day in self.location.workdays]
        workdays = rrule.rrule(
            rrule.DAILY,
            dtstart=start,
            until=end,
            byweekday=week_workdays
        ).count()

        # converting workdays as db expects 1 (Sunday) to 7 (Saturday)
        workdays_db = [
            # special case for Sunday
            int(day) == 7 and 1 or int(day) + 1
            for day in self.location.workdays
        ]
        holidays = PublicHoliday.objects.filter(
            location=self.location,
            date__gte=start,
            date__lte=end,
            date__week_day__in=workdays_db
        ).count()

        expected_worktime = self.worktime_per_day * (workdays - holidays)

        overtime_credit = sum(
            OvertimeCredit.objects.filter(
                user=self.user,
                date__gte=start,
                date__lte=end
            ).values_list('duration', flat=True),
            timedelta()
        )

        reported_worktime = sum(
            Report.objects.filter(
                user=self.user,
                date__gte=start,
                date__lte=end
            ).values_list('duration', flat=True),
            timedelta()
        )

        absences = sum(
            Absence.objects.filter(
                user=self.user,
                date__gte=start,
                date__lte=end
            ).values_list('duration', flat=True),
            timedelta()
        )

        reported = reported_worktime + absences + overtime_credit

        return (reported, expected_worktime, reported - expected_worktime)

    class Meta:
        """Meta information for the employment model."""

        indexes = [models.Index(fields=['start_date', 'end_date'])]


class UserManager(UserManager):
    def all_supervisors(self):
        objects = self.model.objects.annotate(
            supervisees_count=models.Count('supervisees'))
        return objects.filter(supervisees_count__gt=0)

    def all_reviewers(self):
        objects = self.model.objects.annotate(
            reviews_count=models.Count('reviews'))
        return objects.filter(reviews__gt=0)

    def all_supervisees(self):
        objects = self.model.objects.annotate(
            supervisors_count=models.Count('supervisors'))
        return objects.filter(supervisors_count__gt=0)


class User(AbstractUser):
    """Timed specific user."""

    supervisors = models.ManyToManyField('self', symmetrical=False,
                                         related_name='supervisees')

    tour_done = models.BooleanField(default=False)
    """
    Indicate whether user has finished tour through Timed in frontend.
    """

    objects = UserManager()

    def calculate_worktime(self, start, end):
        """Calculate reported, expected and balance for user.

        This calculates summarizes worktime for all employments of users which
        are in given time frame.

        :param start: calculate worktime starting on given day.
        :param end:   calculate worktime till given day
        :returns:     tuple of 3 values reported, expected and balance in given
                      time frame
        """
        employments = Employment.objects.for_user(self, start, end)

        balances = [
            employment.calculate_worktime(start, end)
            for employment in employments
        ]

        reported = sum([balance[0] for balance in balances], timedelta())
        expected = sum([balance[1] for balance in balances], timedelta())
        balance = sum([balance[2] for balance in balances], timedelta())

        return (reported, expected, balance)
