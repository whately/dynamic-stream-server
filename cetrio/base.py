import re
import time
import os
try:
    # Python 3
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen
from concurrent import futures

import noxml
from config import config
import ffmpeg
import streams
import thread_tools
import process_tools


def run_proc(id, cmd, mode):
    """ Open process with error output redirected to file.
        The standart output can be read.

        This should be used as a context manager to close the log file.
    """
    log = os.path.join(config['log']['dir'], '{0}-{1}'.format(mode, id))
    with open(log, 'w') as f:
        return process_tools.Popen(
            cmd,
            stdout=process_tools.PIPE,
            stderr=f
        )


class HTTPClient(object):
    """ Emulate the behaviour of a RTMP client when there's an HTTP access
        for a certain Stream. If no other HTTP access is made within the
        timeout period, the `Stream` instance will be decremented.
    """
    def __init__(self, parent):
        self.lock = thread_tools.Condition()
        self.stopped = True
        self.timeout = None
        self.parent = parent

    def wait(self, timeout):
        self.timeout = timeout
        if not self.stopped:
            with self.lock:
                self.stopped = True
                self.lock.notify_all()
        else:
            self.thread = thread_tools.Thread(self._wait_worker).start()
        return self

    def _wait_worker(self):
        with self.lock:
            while self.stopped:
                self.stopped = False
                self.lock.wait(self.timeout)
            self.stopped = True
            self.parent.dec(http=True)

    def __bool__(self):
        return not self.stopped
    __nonzero__ = __bool__


class Stream(object):
    _ffmpeg =  config['ffmpeg']
    run_timeout = _ffmpeg.getint('timeout')
    reload_timeout = _ffmpeg.getint('reload')

    def __init__(self, id, timeout=run_timeout):
        self.lock = thread_tools.Lock()
        self.id = id
        provider = streams.select_provider(id)
        self.fn = lambda self=self: run_proc(
            self.id,
            provider.make_cmd(self.id),
            'fetch'
        )
        self.cnt = 0
        self._proc_run = False
        self.proc = None
        self.thread = None
        self.timeout = timeout
        self.http_client = HTTPClient(self)

    def __repr__(self):
        pid = self.proc.pid if self.proc else None
        return '<{0}: Users={1} Pid={2}>'.format(self.id, self.clients, pid)

    @property
    def clients(self):
        return self.cnt + bool(self.http_client)

    @property
    def alive(self):
        return self.proc or self.proc_run

    @property
    def proc_run(self):
        return self._proc_run
    
    @proc_run.setter
    def proc_run(self, value):
        with self.lock:
            self._proc_run = value

    def inc(self, k=1, http_wait=None):
        """ Increment user count unless it is a http user (then http_wait
            must be set). If so, it should wait a period of time on another
            thread and the clients property will be indirectly incremented.

            If there is no process running and it should be, a new process
            will be started.
        """
        if http_wait:
            self.http_client.wait(http_wait)
        else:
            self.cnt += k
        if not self.proc and not self.proc_run:
            self.proc_start()
        print(self)
        return self

    def dec(self, http=False):
        """ Decrement the user count unless it is a http user. If there are no
            new clients, the process is scheduled to shutdown.
        """
        if not http:
            if self.cnt:
                self.cnt -= 1
        if not self.clients:
            self.proc_stop()
        print(self)
        return self

    def _proc_msg(self, pid, msg):
        return '{0} - FFmpeg[{1}] {2}'.format(self.id, pid, msg)

    def proc_start(self):
        """ Process starter on another thread.
        """
        def worker():
            self.proc_run = True
            start_msg = 'started'
            while True:
                with self.fn() as self.proc:
                    pid = self.proc and self.proc.pid
                    print(self._proc_msg(pid, start_msg))
                    self.proc.wait()
                    self.proc = None

                    if self.proc_run:  # Should be running, but isn't
                        print(self._proc_msg(pid, 'died'))
                        time.sleep(self.reload_timeout)
                        if self.proc_run:  # It might have been stopped after waiting
                            start_msg = 'restarted'
                            continue
                    print(self._proc_msg(pid, 'stopped'))
                    break

        self.thread = thread_tools.Thread(worker).start()

    def _kill(self):
        """ Kill the FFmpeg process. Don't call this function directly,
            otherwise the process may be restarted. Call `proc_stop` instead.
        """
        try:
            self.proc.kill()
            self.proc.wait()
        except (OSError, AttributeError):
            pass
        finally:
            self.proc = None

    def proc_stop(self, now=False):
        if now:
            self.proc_run = False
            self._kill()
            return

        if not self.proc_run:
            return
        self.proc_run = False

        def stop_worker():
            time.sleep(self.timeout)
            if not self.clients:
                self._kill()
            else:
                self.proc_run = True

        thread_tools.Thread(stop_worker).start()


class Video(object):
    _data = {}
    _data_lock = thread_tools.Lock()
    run = True

    @classmethod
    def start(cls, id, increment=1, http_wait=None):
        if cls.run:
            cls.get_stream(id).inc(increment, http_wait=http_wait)

    @classmethod
    def stop(cls, id):
        cls.get_stream(id).dec()

    @classmethod
    def get_stream(cls, id):
        with cls._data_lock:
            stream = cls._data.get(id)
            if stream is None:
                stream = Stream(id)
                cls._data[id] = stream
            return stream

    @classmethod
    def get_stats(cls):
        http = config['http-server']
        addr = http['addr']
        stat = http['stat_url']
        data = urlopen(addr + stat).read()
        return noxml.load(data)

    @classmethod
    def initialize_from_stats(cls):
        try:
            stats = cls.get_stats()['server']['application']
        except IOError:
            return

        if isinstance(stats, dict): stats = [stats]
        app = config['rtmp-server']['app']
        try:
            app = next(x['live'] for x in stats if x['name'] == app)
        except StopIteration:
            raise NameError('No app named %r' % app)

        # App clients
        stream_list = app.get('stream')
        if stream_list is None:
            return
        if isinstance(stream_list, dict):
            stream_list = [stream_list]

        for stream in stream_list:
            # Stream clients
            nclients = int(stream['nclients'])

            if 'publishing' in stream:
                nclients -= 1

            if nclients <= 0:
                continue

            cls.start(stream['name'], nclients)

    @classmethod
    def terminate_streams(cls):
        with cls._data_lock:
            cls.run = False
            for strm in cls._data.values():
                strm.proc_stop(now=True)


class Thumbnail(object):
    run = True
    clean = True
    lock = thread_tools.Condition()

    stream_list = None
    _thumb = config['thumbnail']
    interval = _thumb.getint('interval')
    workers = _thumb.getint('workers')
    timeout = _thumb.getint('timeout')

    class Worker(object):
        def __init__(self, id, timeout):
            self.id = id
            self.timeout = timeout
            self.proc = None
            self.lock = None

        def _open_proc(self):
            """ Select stream and open process
            """
            provider = streams.select_provider(self.id)
            source = provider.in_stream
            seek = None
            origin = None
            id = self.id

            # Use local connection if stream is already running.
            if Video.get_stream(self.id).alive:
                source = provider.out_stream
                seek = 1
            else:
                # If using remote server identifier instead of local.
                id = provider.get_stream(self.id)
                origin = provider

            return run_proc(
                self.id,
                Thumbnail.make_cmd(id, source, seek, origin),
                'thumb',
            )

        def _close_proc(self):
            """ Kill the open process.
            """
            if self.proc.poll() is not None:
                return
            try:
                self.proc.kill()
                self.proc.wait()
            except OSError:
                pass

        def _waiter(self):
            """ Wait until the first of these events :
                    - Process finished;
                    - Timeout (on another thread);
                    - User request for termination (at the same thread as
                      the timeout).
            """
            with self.lock:
                thread_tools.Condition.wait_for_any(
                    [Thumbnail.lock, self.lock], self.timeout
                )
                self._close_proc()

        def __call__(self):
            """ Opens a new process and sets a waiter with timeout on
                another thread.
                Waits for the end of the process (naturally or killed
                by waiter). Awakes the waiter if process finished first.
                Returns the process output code.
            """
            self.lock = thread_tools.Condition.from_condition(Thumbnail.lock)
            with self.lock:
                if not Thumbnail.run:
                    return

            with self._open_proc() as self.proc:
                thread_tools.Thread(self._waiter).start()

                self.proc.communicate()
                with self.lock:
                    self.lock.notify_all()

                return self.proc.poll()

    @classmethod
    def main_worker(cls):
        stream_list = [p.streams() for p in streams.providers.values()]
        cls.stream_list = [item for sublist in stream_list for item in sublist]

        try:
            delay = cls._thumb.getint('start_after')
        except Exception:
            delay = 0
        with cls.lock:
            cls.lock.wait(delay)

        while True:
            with cls.lock:
                if not cls.run:
                    break
                cls.clean = False
            t = time.time()
            with futures.ThreadPoolExecutor(cls.workers) as executor:
                map = dict(
                    (executor.submit(cls.Worker(x, cls.timeout)), x)
                    for x in cls.stream_list
                )
                done = {}
                for future in futures.as_completed(map):
                    done[map[future]] = future.result()
                error = [x for x in cls.stream_list if done[x] != 0]

                if cls.run: # Show stats
                    cams = len(cls.stream_list)
                    print('Finished fetching thumbnails: {0}/{1}'.format(cams - len(error), cams))
                    if error:
                        print('Could not fetch:\n' + ', '.join(error))

            t = time.time() - t
            interval = cls.interval - t
            with cls.lock:
                cls.clean = True
                cls.lock.notify_all()

                if interval >= 0:
                    cls.lock.wait(interval)
                elif cls.run:
                    print('Thumbnail round delayed by {0:.2f} seconds'.format(-interval))


    @classmethod
    def make_cmd(cls, name, source, seek=None, origin=None):
        """ Generate FFmpeg command for thumbnail generation.
        """
        thumb = cls._thumb
        out_opt = thumb['output_opt']
        if seek is not None:
            out_opt += ' -ss ' + str(seek)

        resize_opt = thumb['resize_opt']
        sizes = re.findall(r'(\w+):(\w+)', thumb['sizes'])

        resize = [''] + [resize_opt.format(s[1]) for s in sizes]
        names = [''] + ['-' + s[0] for s in sizes]

        # If fetching thumbnail from origin server, will need the stream
        # id that is different from stream name.
        id = name
        if origin:
            id = origin.get_id(name)

        outputs = [
            os.path.join(
                thumb['dir'],
                '{0}{1}.{2}'.format(id, _name, thumb['format'])
            )
            for _name in names
        ]

        return ffmpeg.cmd_outputs(
            thumb['input_opt'],
            source.format(name),
            out_opt,
            resize,
            outputs
        )

    @classmethod
    def start_download(cls):
        thread_tools.Thread(cls.main_worker).start()

    @classmethod
    def stop_download(cls):
        with cls.lock:
            cls.run = False
            cls.lock.notify_all()
            while not cls.clean:
                cls.lock.wait()
