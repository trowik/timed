import Service  from 'ember-service'
import service  from 'ember-service/inject'
import moment   from 'moment'
import computed from 'ember-computed-decorators'

import {
  task,
  timeout
} from 'ember-concurrency'

export default Service.extend({
  /**
   * The store service
   *
   * @property {Ember.Store} store
   * @public
   */
  store: service('store'),

  /**
   * The notify service
   *
   * @property {EmberNotify.NotifyService} notify
   * @public
   */
  notify: service('notify'),

  async init() {
    this._super()

    let actives = await this.get('store').query('activity', {
      include: 'blocks,task,task.project,task.project.customer',
      active: true
    })

    this.set('activity', actives.getWithDefault('firstObject', null))
  },

  filterCustomers: task(function* () {
    yield timeout(500)

    let customers = yield this.get('store').query('customer', {})

    return customers
  }).restartable(),

  filterProjects: task(function* (customer) {
    if (!customer) {
      throw new Error('No customer selected')
    }

    yield timeout(500)

    let projects = yield this.get('store').query('project', { customer })

    return projects
  }).restartable(),

  filterTasks: task(function* (project) {
    if (!project) {
      throw new Error('No project selected')
    }

    yield timeout(500)

    let tasks = yield this.get('store').query('task', { project })

    return tasks
  }).restartable(),

  /**
   * The current activity
   *
   * @property {Activity} currentActivity
   * @public
   */
  _activity: null,

  @computed
  activity: {
    get() {
      return this.get('_activity')
    },
    set(key, value) {
      let newActivity = value || this.get('store').createRecord('activity')

      this.set('_activity', newActivity)

      return newActivity
    }
  },

  /**
   * Start the activity
   *
   * @method startActivity
   * @public
   */
  startActivity: task(function* () {
    let activity = this.get('activity')

    if (activity.get('active')) {
      return
    }

    try {
      if (activity.get('isNew')) {
        yield activity.save()
      }

      let block = this.get('store').createRecord('activity-block', { activity })

      yield block.save()

      this.get('notify').success('Activity was started')
    }
    catch(e) {
      /* istanbul ignore next */
      this.get('notify').error('Error while starting the activity')
    }
  }).drop(),

  /**
   * Stop the activity
   *
   * @method stopActivity
   * @public
   */
  stopActivity: task(function* () {
    let activity = this.get('activity')

    if (!activity.get('active')) {
      return
    }

    try {
      let block = yield activity.get('activeBlock')

      if (block) {
        block.set('to', moment())

        yield block.save()
      }

      this.set('activity', null)

      this.get('notify').success('Activity was stopped')
    }
    catch(e) {
      /* istanbul ignore next */
      this.get('notify').error('Error while stopping the activity')
    }
  }).drop()
})
