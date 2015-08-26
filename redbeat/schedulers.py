# Licensed under the Apache License, Version 2.0 (the 'License'); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
# Copyright 2014 Kong Luoxing, Copyright 2015 Marc Sibson


import datetime
from copy import deepcopy
import time

try:
    import simplejson as json
except ImportError:
    import json

from celery.beat import Scheduler, ScheduleEntry
from celery.utils.log import get_logger
from celery import current_app
import celery.schedules

from redis.client import StrictRedis
from redis.exceptions import ResponseError

from decoder import DateTimeDecoder, DateTimeEncoder

# share with result backend
rdb = StrictRedis.from_url(current_app.conf.REDBEAT_REDIS_URL)

REDBEAT_DELETES_KEY = current_app.conf.REDBEAT_KEY_PREFIX + ':deletes'
REDBEAT_UPDATES_KEY = current_app.conf.REDBEAT_KEY_PREFIX + ':updates'
REDBEAT_SCHEDULE_KEY = current_app.conf.REDBEAT_KEY_PREFIX + ':schedule'

ADD_ENTRY_ERROR = """\

Couldn't add entry %r to redis schedule: %r. Contents: %r
"""

logger = get_logger(__name__)


def to_timestamp(dt):
    return time.mktime(dt.timetuple())


class ValidationError(Exception):
    pass


class PeriodicTask(object):
    '''represents a periodic task
    '''
    name = None
    task = None

    type_ = None

    interval = None
    crontab = None

    args = []
    kwargs = {}

    queue = None
    exchange = None
    routing_key = None

    # datetime
    expires = None
    enabled = True

    date_changed = None
    description = None

    no_changes = False

    def __init__(self, name, task, schedule, key=None, enabled=True, task_args=[], task_kwargs={}, **kwargs):
        self.task = task
        self.enabled = enabled
        if isinstance(schedule, self.Interval):
            self.interval = schedule
        if isinstance(schedule, self.Crontab):
            self.crontab = schedule
        self.args = task_args
        self.kwargs = task_kwargs

        if not key:
            self.name = current_app.conf.REDBEAT_KEY_PREFIX + name
        else:
            self.name = current_app.conf.REDBEAT_KEY_PREFIX + name + ':' + key

    class Interval(object):

        def __init__(self, every, period='seconds'):
            self.every = every
            # could be seconds minutes hours
            self.period = period

        @property
        def schedule(self):
            return celery.schedules.schedule(datetime.timedelta(**{self.period: self.every}))

        @property
        def period_singular(self):
            return self.period[:-1]

        def __unicode__(self):
            if self.every == 1:
                return 'every {0.period_singular}'.format(self)
            return 'every {0.every} {0.period}'.format(self)

        __str__ = __unicode__

    class Crontab(object):

        def __init__(self, minute, hour, day_of_week, day_of_month, month_of_year):
            self.minute = minute
            self.hour = hour
            self.day_of_week = day_of_week
            self.day_of_month = day_of_month
            self.month_of_year = month_of_year

        @property
        def schedule(self):
            return celery.schedules.crontab(minute=self.minute,
                                            hour=self.hour,
                                            day_of_week=self.day_of_week,
                                            day_of_month=self.day_of_month,
                                            month_of_year=self.month_of_year)

        def __unicode__(self):
            rfield = lambda f: f and str(f).replace(' ', '') or '*'
            return '{0} {1} {2} {3} {4} (m/h/d/dM/MY)'.format(
                rfield(self.minute), rfield(self.hour), rfield(self.day_of_week),
                rfield(self.day_of_month), rfield(self.month_of_year),
            )
        __str__ = __unicode__

    @staticmethod
    def get_all():
        """get all of the tasks, for best performance with large amount of tasks, return a generator
        """
        tasks = rdb.scan_iter(match='{}*'.format(current_app.conf.REDBEAT_KEY_PREFIX))
        # filter out internal keys
        tasks = (t for t in tasks if ':' not in t.replace(current_app.conf.REDBEAT_KEY_PREFIX, ''))
        return tasks

    @staticmethod
    def load(task_name):
        try:
            raw = rdb.hget(task_name, 'periodic')
        except ResponseError as exc:
            if 'WRONGTYPE' in exc.message:
                raw = rdb.get(task_name)
            else:
                raise

        if not raw:
            raise KeyError(task_name)

        return json.loads(raw, cls=DateTimeDecoder)

    @staticmethod
    def from_key(task_name):
        return PeriodicTask.from_dict(PeriodicTask.load(task_name))

    @staticmethod
    def from_dict(d):
        """
        build PeriodicTask instance from dict
        :param d: dict
        :return: PeriodicTask instance
        """
        if d.get('interval'):
            schedule = PeriodicTask.Interval(d['interval']['every'], d['interval']['period'])
        if d.get('crontab'):
            schedule = PeriodicTask.Crontab(
                d['crontab']['minute'],
                d['crontab']['hour'],
                d['crontab']['day_of_week'],
                d['crontab']['day_of_month'],
                d['crontab']['month_of_year']
            )
        task = PeriodicTask(d['name'], d['task'], schedule)
        for key in d:
            if key not in ('interval', 'crontab', 'schedule'):
                setattr(task, key, d[key])
        return task

    def delete(self):
        rdb.sadd(REDBEAT_DELETES_KEY, self.name)
        rdb.delete(self.name)

    def save(self):
        self.clean()

        # must do a deepcopy
        self_dict = deepcopy(self.__dict__)
        if self_dict.get('interval'):
            self_dict['interval'] = self.interval.__dict__
        if self_dict.get('crontab'):
            self_dict['crontab'] = self.crontab.__dict__

        rdb.srem(REDBEAT_DELETES_KEY, self.name)
        rdb.hset(self.name, 'periodic', json.dumps(self_dict, cls=DateTimeEncoder))
        rdb.sadd(REDBEAT_UPDATES_KEY, self.name)

    def clean(self):
        """validation to ensure that you only have
        an interval or crontab schedule, but not both simultaneously"""
        if self.interval and self.crontab:
            msg = 'Cannot define both interval and crontab schedule.'
            raise ValidationError(msg)
        if not (self.interval or self.crontab):
            msg = 'Must defined either interval or crontab schedule.'
            raise ValidationError(msg)

    @property
    def schedule(self):
        if self.interval:
            return self.interval.schedule
        elif self.crontab:
            return self.crontab.schedule
        else:
            raise Exception('must define interval or crontab schedule')

    def __unicode__(self):
        fmt = '{0.name}: {{no schedule}}'
        if self.interval:
            fmt = '{0.name}: {0.interval}'
        elif self.crontab:
            fmt = '{0.name}: {0.crontab}'
        else:
            raise Exception('must define internal or crontab schedule')
        return fmt.format(self)

    __str__ = __unicode__


class RedBeatSchedulerEntry(ScheduleEntry):
    def __init__(self, task, total_run_count=0, last_run_at=None):
        self._task = task

        self.app = current_app._get_current_object()
        self.name = self._task.name
        self.task = self._task.task

        self.schedule = self._task.schedule

        self.args = self._task.args
        self.kwargs = self._task.kwargs
        self.options = {
            'queue': self._task.queue,
            'exchange': self._task.exchange,
            'routing_key': self._task.routing_key,
            'expires': self._task.expires
        }

        if not total_run_count and not last_run_at:
            meta = RedBeatSchedulerEntry.load(self.name)
            self.total_run_count = meta.get('total_run_count', 0)
            self.last_run_at = meta.get('last_run_at', self._default_now())

    @staticmethod
    def load(task_name):
        try:
            meta = rdb.hget(task_name, 'meta')
        except ResponseError as exc:
            if 'WRONGTYPE' in str(exc):
                return {}

        if not meta:
            return {}

        return json.loads(meta, cls=DateTimeDecoder)

    @staticmethod
    def from_key(task_name):
        task = PeriodicTask.from_key(task_name)
        return RedBeatSchedulerEntry(task)

    @property
    def due_at(self):
        delta = self.schedule.remaining_estimate(self.last_run_at)
        due_at = self.last_run_at + delta
        return to_timestamp(due_at)

    def next(self):
        self.last_run_at = self._default_now()
        self.total_run_count += 1
        self.save()

        return self

    __next__ = next

    def is_due(self):
        if not self._task.enabled:
            return False, 5.0  # 5 second delay for re-enable.

        return super(RedBeatSchedulerEntry, self).is_due()

    def __repr__(self):
        return '<RedBeatSchedulerEntry ({0} {1}(*{2}, **{3}) {{4}})>'.format(
            self.name, self.task, self.args, self.kwargs, self.schedule,
        )

    def save(self):
        meta = {
            'last_run_at': self.last_run_at,
            'total_run_count': self.total_run_count,
        }
        rdb.hset(self.name, 'meta', json.dumps(meta, cls=DateTimeEncoder))

    @classmethod
    def from_entry(cls, name, skip_fields=('relative', 'options'), **entry):
        options = entry.get('options') or {}
        fields = dict(entry)
        for skip_field in skip_fields:
            fields.pop(skip_field, None)
        fields['name'] = current_app.conf.REDBEAT_KEY_PREFIX + name
        schedule = fields.pop('schedule')
        schedule = celery.schedules.maybe_schedule(schedule)
        if isinstance(schedule, celery.schedules.crontab):
            fields['crontab'] = {
                'minute': schedule._orig_minute,
                'hour': schedule._orig_hour,
                'day_of_week': schedule._orig_day_of_week,
                'day_of_month': schedule._orig_day_of_month,
                'month_of_year': schedule._orig_month_of_year
            }
        elif isinstance(schedule, celery.schedules.schedule):
            fields['interval'] = {'every': max(schedule.run_every.total_seconds(), 0), 'period': 'seconds'}

        fields['args'] = fields.get('args', [])
        fields['kwargs'] = fields.get('kwargs', {})
        fields['queue'] = options.get('queue')
        fields['exchange'] = options.get('exchange')
        fields['routing_key'] = options.get('routing_key')
        return cls(PeriodicTask.from_dict(fields))


class RedBeatScheduler(Scheduler):
    # how often should we sync in schedule information
    # from the backend redis database
    Entry = RedBeatSchedulerEntry

    def setup_schedule(self):
        self.install_default_entries(self.app.conf.CELERYBEAT_SCHEDULE)
        self.update_from_dict(self.app.conf.CELERYBEAT_SCHEDULE)
        self.load_from_database()

    def load_from_database(self):
        logger.info('Reading PeriodicTasks from Redis')
        for task in PeriodicTask.get_all():
            try:
                t = PeriodicTask.from_key(task)
            except KeyError:
                pass
            else:
                logger.debug(unicode(t))
                entry = self.Entry(t)
                rdb.zadd(REDBEAT_SCHEDULE_KEY, entry.due_at, entry.name)

    def update_from_dict(self, dict_):
        for name, entry in dict_.items():
            try:
                entry = self.Entry.from_entry(name, **entry)
            except Exception as exc:
                logger.error(ADD_ENTRY_ERROR, name, exc, entry)

            entry._task.save()
            rdb.srem(REDBEAT_UPDATES_KEY, entry.name)  # hack, avoid triggering task updat
            logger.debug(unicode(entry._task))
            rdb.zadd(REDBEAT_SCHEDULE_KEY, entry.due_at, entry.name)

    def reserve(self, entry):
        new_entry = next(entry)
        rdb.zadd(REDBEAT_SCHEDULE_KEY, new_entry.due_at, new_entry.name)

        return new_entry

    def schedule_changed(self):
        """check whether we should pull an updated schedule
        from the backend database"""
        return rdb.scard(REDBEAT_DELETES_KEY) or rdb.scard(REDBEAT_UPDATES_KEY)

    def update_schedule(self):
        delete = rdb.spop(REDBEAT_DELETES_KEY)
        while delete:
            logger.debug('deleting %s', delete)
            rdb.zrem(REDBEAT_SCHEDULE_KEY, delete)
            delete = rdb.spop(REDBEAT_DELETES_KEY)

        update = rdb.spop(REDBEAT_UPDATES_KEY)
        while update:
            logger.debug('updating %s', update)
            try:
                entry = self.Entry.from_key(update)
                rdb.zadd(REDBEAT_SCHEDULE_KEY, entry.due_at, entry.name)
            except KeyError:  # got deleted before we tried to update
                pass

            update = rdb.spop(REDBEAT_UPDATES_KEY)

    @property
    def schedule(self):
        if self.schedule_changed():
            self.update_schedule()

        maxscore = to_timestamp(self.app.now())
        due_tasks = rdb.zrangebyscore(REDBEAT_SCHEDULE_KEY, 0, maxscore)
        return {key: self.Entry.from_key(key) for key in due_tasks}
