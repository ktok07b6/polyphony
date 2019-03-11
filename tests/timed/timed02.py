from polyphony import module, rule
from polyphony import testbench
from polyphony.timing import timed, clkfence, wait_value
from polyphony.io import Port, Handshake


@module
class timed02:
    def __init__(self):
        self.i = Handshake(int, 'in')
        self.o = Handshake(int, 'out')
        self.append_worker(self.w)

    @rule(scheduling='timed')
    def w(self):
        x = self.i.rd()
        assert 3 == x
        x = self.i.rd()
        assert 5 == x

        clkfence()
        self.o.wr(10)
        clkfence()
        clkfence()
        clkfence()

        self.o.wr(20)
        clkfence()


@rule(scheduling='timed')
@testbench
def test(m):
    clkfence()
    clkfence()
    m.i.wr(3)
    m.i.wr(5)
    clkfence()
    clkfence()
    clkfence()

    assert 10 == m.o.rd()
    assert 20 == m.o.rd()
    clkfence()
    clkfence()


m = timed02()
test(m)
