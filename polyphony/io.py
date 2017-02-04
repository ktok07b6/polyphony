import queue, time, sys, pdb
import threading

__all__ = [
    'Bit',
    'Int',
    'Uint'
]

def _init_io():
    if sys.getswitchinterval() > 0.005:
        sys.setswitchinterval(0.005) # 5ms

_events = []
_conds = []
_io_enabled = False

def _create_event():
    ev = threading.Event()
    _events.append(ev)
    return ev

def _create_cond():
    cv = threading.Condition()
    _conds.append(cv)
    return cv

def _remove_cond(cv):
    _conds.remove(cv)

def _enable():
    global _io_enabled
    _io_enabled = True

def _disable():
    global _io_enabled
    _io_enabled = False
    for ev in _events:
        ev.set()
    for cv in _conds:
        with cv:
            cv.notify_all()

class PolyphonyException(Exception):
    pass

class PolyphonyIOException(PolyphonyException):
    pass

def _portmethod(func):
    def _portmethod_decorator(*args, **kwargs):
        if not _io_enabled:
            raise PolyphonyIOException()
        return func(*args, **kwargs)
    return _portmethod_decorator

class _DataPort:
    def __init__(self, init_v:int=0, width:int=1, protocol:int='none') -> object:
        self.__v = init_v
        self.__oldv = 0
        self.__cv = []
        self.__cv_lock = threading.Lock()

    @_portmethod
    def rd(self) -> int:
        return self.__v

    @_portmethod
    def wr(self, v):
        if not self.__cv:
            self.__oldv = self.__v
            self.__v = v
        else:
            with self.__cv_lock:
                self.__oldv = self.__v
                self.__v = v
                for cv in self.__cv:
                    with cv:
                        cv.notify_all()
        time.sleep(0.005)

    def __call__(self, v=None) -> int:
        if v is None:
            return self.rd()
        else:
            self.wr(v)

    def _add_cv(self, cv):
        with self.__cv_lock:
            self.__cv.append(cv)

    def _del_cv(self, cv):
        with self.__cv_lock:
            self.__cv.remove(cv)

    def _rd_old(self):
        return self.__oldv


class Bit(_DataPort):
    def __init__(self, init_v:int=0, width:int=1, protocol:int='none') -> object:
        super().__init__(init_v, width, protocol)

class Int(_DataPort):
    def __init__(self, width:int=32, init_v:int=0, protocol:int='none') -> object:
        super().__init__(init_v, width, protocol)

class Uint(_DataPort):
    def __init__(self, width:int=32, init_v:int=0, protocol:int='none') -> object:
        super().__init__(init_v, width, protocol)

class Queue:
    def __init__(self, width:int=32, max_size:int=0, name='') -> object:
        self.__width = width
        self.__q = queue.Queue(max_size)
        self.__ev_put = _create_event()
        self.__ev_get = _create_event()
        self.__name = name

    @_portmethod
    def rd(self) -> int:
        while self.__q.empty():
            self.__ev_put.wait()
            if _io_enabled:
                self.__ev_put.clear()
            else:
                return 0
        d = self.__q.get(block=False)

        self.__ev_get.set()
        return d

    @_portmethod
    def wr(self, v):
        while self.__q.full():
            self.__ev_get.wait()
            if _io_enabled:
                self.__ev_get.clear()
            else:
                return
            #time.sleep(0.001)
        self.__q.put(v, block=False)
        self.__ev_put.set()

    def __call__(self, v=None) -> int:
        if v is None:
            return self.rd()
        else:
            self.wr(v)

    @_portmethod
    def empty(self):
        return self.__q.empty()

    @_portmethod
    def full(self):
        return self.__q.full()

_init_io()
