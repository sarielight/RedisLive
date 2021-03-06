from api.util import settings
from datetime import datetime, timedelta
import cyclone.redis
import json
import ast
from twisted.internet import defer
from twisted.python import log

class TxRedisStatsProvider(object):
    """A Redis based persistance to store and fetch stats"""

    def __init__(self):
        # redis server to use to store stats
        stats_server = settings.get_redis_stats_server()
        self.server = stats_server["server"]
        self.port = stats_server["port"]
        self.dbid = 0
        self.poolsize = 200
        self.conn = cyclone.redis.lazyConnectionPool(self.server, self.port, self.dbid, self.poolsize)

    @defer.inlineCallbacks
    def save_memory_info(self, server, timestamp, used, peak):
        """Saves used and peak memory stats,

        Args:
            server (str): The server ID
            timestamp (datetime): The time of the info.
            used (int): Used memory value.
            peak (int): Peak memory value.
        """
        data = {"timestamp": timestamp.strftime('%s'),
                "used": used,
                "peak": peak}
        yield self.conn.zadd(server + ":memory", timestamp.strftime('%s'), data)

    @defer.inlineCallbacks
    def save_info_command(self, server, timestamp, info):
        """Save Redis info command dump

        Args:
            server (str): id of server
            timestamp (datetime): Timestamp.
            info (dict): The result of a Redis INFO command.
        """
        yield self.conn.set(server + ":Info", json.dumps(info))

    @defer.inlineCallbacks
    def save_monitor_command(self, server, timestamp, command, keyname,
                             argument):
        """save information about every command

        Args:
            server (str): Server ID
            timestamp (datetime): Timestamp.
            command (str): The Redis command used.
            keyname (str): The key the command acted on.
            argument (str): The args sent to the command.
        """

        epoch = timestamp.strftime('%s')
        current_date = timestamp.strftime('%y%m%d')

        # start a redis MULTI/EXEC transaction
        pipeline = yield self.conn.multi()
        log.msg('.')
        # store top command and key counts in sorted set for every second
        # top N are easily available from sorted set in redis
        # also keep a sorted set for every day
        # switch to daily stats when stats requsted are for a longer time period        
        try:
            command_count_key = server + ":CommandCount:" + epoch
            yield pipeline.zincrby(command_count_key, command, 1)

            command_count_key = server + ":DailyCommandCount:" + current_date
            yield pipeline.zincrby(command_count_key, command, 1)
            
            key_count_key = server + ":KeyCount:" + epoch
            yield pipeline.zincrby(key_count_key, keyname, 1)

            key_count_key = server + ":DailyKeyCount:" + current_date
            yield pipeline.zincrby(key_count_key, command, 1)

            # keep aggregate command in a hash
            command_count_key = server + ":CommandCountBySecond"
            yield pipeline.hincrby(command_count_key, epoch, 1)

            command_count_key = server + ":CommandCountByMinute"
            field_name = current_date + ":" + str(timestamp.hour) + ":"
            field_name += str(timestamp.minute)
            yield pipeline.hincrby(command_count_key, field_name, 1)

            command_count_key = server + ":CommandCountByHour"
            field_name = current_date + ":" + str(timestamp.hour)
            yield pipeline.hincrby(command_count_key, field_name, 1)

            command_count_key = server + ":CommandCountByDay"
            field_name = current_date
            yield pipeline.hincrby(command_count_key, field_name, 1)
        except Exception, e:
            log.msg("Provider Exception: " % e)
            pipeline.discard()

        r = yield pipeline.commit()
        log.msg("transaction: %s" % r)

    @defer.inlineCallbacks
    def get_info(self, server):
        """Get info about the server

        Args:
            server (str): The server ID
        """
        info = yield self.conn.get(server + ":Info")
        # FIXME: If the collector has never been run we get a 500 here. `None`
        # is not a valid type to pass to json.loads.
        info = json.loads(info)
        defer.returnValue(info)

    @defer.inlineCallbacks
    def get_memory_info(self, server, from_date, to_date):
        """Get stats for Memory Consumption between a range of dates

        Args:
            server (str): The server ID
            from_date (datetime): Get memory info from this date onwards.
            to_date (datetime): Get memory info up to this date.
        """
        memory_data = []
        start = int(from_date.strftime("%s"))
        end = int(to_date.strftime("%s"))
        rows = yield self.conn.zrangebyscore(server + ":memory", start, end)

        for row in rows:
            # TODO: Check to see if there's not a better way to do this. Using
            # eval feels like it could be wrong/dangerous... but that's just a
            # feeling.
            row = ast.literal_eval(row) #TODO
            parts = []

            # convert the timestamp
            timestamp = datetime.fromtimestamp(int(row['timestamp']))
            timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')

            memory_data.append([timestamp, row['peak'], row['used']])

        defer.returnValue(memory_data)

    @defer.inlineCallbacks
    def get_command_stats(self, server, from_date, to_date, group_by):
        """Get total commands processed in the given time period

        Args:
            server (str): The server ID
            from_date (datetime): Get data from this date.
            to_date (datetime): Get data to this date.
            group_by (str): How to group the stats.
        """
        s = []
        time_stamps = []
        key_name = ""

        if group_by == "day":
            key_name = server + ":CommandCountByDay"
            t = from_date.date()
            while t <= to_date.date():
                s.append(t.strftime('%y%m%d'))
                time_stamps.append(t.strftime('%s'))
                t = t + timedelta(days=1)

        elif group_by == "hour":
            key_name = server + ":CommandCountByHour"

            t = from_date
            while t<= to_date:
                field_name = t.strftime('%y%m%d') + ":" + str(t.hour)
                s.append(field_name)
                time_stamps.append(t.strftime('%s'))
                t = t + timedelta(seconds=3600)

        elif group_by == "minute":
            key_name = server + ":CommandCountByMinute"

            t = from_date
            while t <= to_date:
                field_name = t.strftime('%y%m%d') + ":" + str(t.hour)
                field_name += ":" + str(t.minute)
                s.append(field_name)
                time_stamps.append(t.strftime('%s'))
                t = t + timedelta(seconds=60)

        else:
            key_name = server + ":CommandCountBySecond"
            start = int(from_date.strftime("%s"))
            end = int(to_date.strftime("%s"))
            for x in range(start, end + 1):
                s.append(str(x))
                time_stamps.append(x)

        data = []
        counts = yield self.conn.hmget(key_name, s)
        for x in xrange(0,len(counts)):
            # the default time format string
            time_fmt = '%Y-%m-%d %H:%M:%S'

            if group_by == "day":
                time_fmt = '%Y-%m-%d'
            elif group_by == "hour":
                time_fmt = '%Y-%m-%d %H:00:00'
            elif group_by == "minute":
                time_fmt = '%Y-%m-%d %H:%M:00'

            # get the count.
            try:
                if counts[x] is not None: 
                    count = int(counts[x])
                else:
                    count = 0
            except Exception as e:
                count = 0

            # convert the timestamp
            timestamp = int(time_stamps[x])
            timestamp = datetime.fromtimestamp(timestamp)
            timestamp = timestamp.strftime(time_fmt)

            # add to the data
            data.append([count, timestamp])
        defer.returnValue(reversed(data))

    @defer.inlineCallbacks
    def get_top_commands_stats(self, server, from_date, to_date):
        """Get top commands processed in the given time period

        Args:
            server (str): Server ID
            from_date (datetime): Get stats from this date.
            to_date (datetime): Get stats to this date.
        """

        counts = self.get_top_counts(server, from_date, to_date, "CommandCount",
                                     "DailyCommandCount")
        defer.returnValue(reversed(counts))

    @defer.inlineCallbacks
    def get_top_keys_stats(self, server, from_date, to_date):
        """Gets top comm processed

        Args:
            server (str): Server ID
            from_date (datetime): Get stats from this date.
            to_date (datetime): Get stats to this date.
        """
        defer.returnValue(self.get_top_counts(server, from_date, to_date, "KeyCount",
                                   "DailyKeyCount"))


    # Helper methods

    @defer.inlineCallbacks
    def get_top_counts(self, server, from_date, to_date, seconds_key_name,
                       day_key_name, result_count=None):
        """Top counts are stored in a sorted set for every second and for every
        day. ZUNIONSTORE across the timeperiods generates the results.

        Args:
            server (str): The server ID
            from_date (datetime): Get stats from this date.
            to_date (datetime): Get stats to this date.
            seconds_key_name (str): The key for stats at second resolution.
            day_key_name (str): The key for stats at daily resolution.

        Kwargs:
            result_count (int): The number of results to return. Default: 10
        """
        if result_count is None:
            result_count = 10

        # get epoch
        start = int(from_date.strftime("%s"))
        end = int(to_date.strftime("%s"))
        diff = to_date - from_date

        # start a redis MULTI/EXEC transaction
        pipeline = yield self.conn.multi()

        # store the set names to use in ZUNIONSTORE in a list
        s = []

        if diff.days > 2 :
            # when difference is over 2 days, no need to check counts for every second
            # Calculate:
            # counts of every second on the start day
            # counts of every day in between
            # counts of every second on the end day
            next_day = from_date.date() + timedelta(days=1)
            prev_day = to_date.date() - timedelta(days=1)
            from_date_end_epoch = int(next_day.strftime("%s")) - 1
            to_date_begin_epoch = int(to_date.date().strftime("%s"))

            # add counts of every second on the start day
            for x in range(start, from_date_end_epoch + 1):
                s.append(":".join([server, seconds_key_name, str(x)]))

            # add counts of all days in between
            t = next_day
            while t <= prev_day:
                s.append(":".join([server, day_key_name, t.strftime('%y%m%d')]))
                t = t + timedelta(days=1)

            # add counts of every second on the end day
            for x in range(to_date_begin_epoch, end + 1):
                s.append(server + ":" + seconds_key_name + ":" + str(x))

        else:
            # add counts of all seconds between start and end date
            for x in range(start, end + 1):
                s.append(server + ":" + seconds_key_name + ":" + str(x))

        # store the union of all the sets in a temp set
        temp_key_name = "_top_counts"
        yield pipeline.zunionstore(temp_key_name, s)
        yield pipeline.zrange(temp_key_name, 0, result_count - 1, True)
        yield pipeline.delete(temp_key_name)

        # commit transaction to redis
        results = yield pipeline.commit()
        print results
        result_data = []
        l = results[-2]
        res = [(l[n], l[n+1]) for n in range(0, len(l), 2)]
        for val, count in res:
            result_data.append([val, count])

        defer.returnValue(result_data)
