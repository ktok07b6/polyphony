﻿from collections import defaultdict
from .env import env
from .ir import Ctx, CONST
from .ahdl import *
from .ahdlvisitor import AHDLVisitor
from .hdlmodule import FIFOModule
from .hdlmemport import HDLMemPortMaker, HDLTuplePortMaker, HDLRegArrayPortMaker
from .hdlinterface import *
from .memref import *
from logging import getLogger
logger = getLogger(__name__)


class HDLModuleBuilder(object):
    @classmethod
    def create(cls, hdlmodule):
        if hdlmodule.scope.is_module():
            return HDLTopModuleBuilder()
        elif hdlmodule.scope.is_testbench():
            return HDLTestbenchBuilder()
        elif hdlmodule.scope.is_function_module():
            return HDLFunctionModuleBuilder()
        else:
            assert False

    def process(self, hdlmodule):
        self.hdlmodule = hdlmodule
        self._build_module()

    def _add_internal_ports(self, locals):
        regs = []
        nets = []
        for sig in locals:
            sig = self.hdlmodule.gen_sig(sig.name, sig.width, sig.tags)
            if sig.is_memif() or sig.is_ctrl() or sig.is_extport():
                continue
            else:
                assert ((sig.is_net() and not sig.is_reg()) or
                        (not sig.is_net() and sig.is_reg()) or
                        (not sig.is_net() and not sig.is_reg()))
                if sig.is_net():
                    self.hdlmodule.add_internal_net(sig)
                    nets.append(sig)
                elif sig.is_reg():
                    self.hdlmodule.add_internal_reg(sig)
                    regs.append(sig)
        return regs, nets

    def _add_state_register(self, fsm):
        state_sig = self.hdlmodule.gen_sig(fsm.name + '_state',
                                           -1,
                                           ['statevar'])
        self.hdlmodule.add_fsm_state_var(fsm.name, state_sig)
        self.hdlmodule.add_internal_reg(state_sig)

    def _add_callee_submodules(self, scope):
        for callee_scope, inst_names in scope.callee_instances.items():
            if callee_scope.is_port():
                continue
            if callee_scope.is_lib():
                continue
            inst_scope_name = callee_scope.base_name
            # TODO: add primitive function hook here
            if inst_scope_name == 'print':
                continue
            self._add_submodule_instances(env.hdlmodule(callee_scope), inst_names, {})

    def _add_submodule_instances(self, sub_hdlmodule, inst_names, param_map, is_internal=False):
        for inst_name in inst_names:
            connections = defaultdict(list)
            for sub_module_inf in sub_hdlmodule.interfaces.values():
                if is_internal:
                    acc = sub_module_inf.accessor('')
                else:
                    acc = sub_module_inf.accessor(inst_name)
                    self._add_external_accessor_for_submodule(sub_module_inf, acc)
                if isinstance(sub_module_inf, WriteInterface):
                    connections['ret'].append((sub_module_inf, acc))
                else:
                    connections[''].append((sub_module_inf, acc))
            self.hdlmodule.add_sub_module(inst_name,
                                          sub_hdlmodule,
                                          connections,
                                          param_map=param_map)

    def _add_external_accessor_for_submodule(self, sub_module_inf, acc):
        if not isinstance(acc, CallAccessor) and acc.acc_name not in self.hdlmodule.signals:
            # we have never accessed this interface
            return
        self.hdlmodule.add_accessor(acc.acc_name, acc)

    def _add_roms(self, memnodes):
        mrg = env.memref_graph
        roms = [n for n in memnodes if mrg.is_readonly_sink(n)]
        while roms:
            memnode = roms.pop()
            output_sig = self.hdlmodule.signal(memnode.sym)
            if not output_sig:
                # In this case memnode is not used at all
                # so we do not declare rom function
                continue
            fname = AHDL_VAR(output_sig, Ctx.STORE)
            input_sig = self.hdlmodule.gen_sig(output_sig.name + '_in', memnode.addr_width())
            input = AHDL_VAR(input_sig, Ctx.LOAD)

            source = memnode.single_source()
            if source:
                array = source.initstm.src
                case_items = []
                for i, item in enumerate(array.items):
                    assert item.is_a(CONST)
                    connect = AHDL_BLOCK(str(i), [AHDL_CONNECT(fname, AHDL_CONST(item.value))])
                    case_items.append(AHDL_CASE_ITEM(i, connect))
                case = AHDL_CASE(input, case_items)
                rom_func = AHDL_FUNCTION(fname, [input], [case])
            else:
                cs_name = output_sig.name + '_cs'
                cs_sig = self.hdlmodule.signal(cs_name)
                assert cs_sig
                cs = AHDL_VAR(cs_sig, Ctx.LOAD)

                case_items = []
                n2o = memnode.pred_branch()
                for i, pred in enumerate(n2o.preds):
                    pred_root = mrg.find_nearest_single_source(pred)
                    assert len(pred_root.succs) == 1
                    for romsrc in pred_root.succs[0].succs:
                        if romsrc.is_sink():
                            break
                    else:
                        assert False
                    roms.append(romsrc)
                    rom_func_name = romsrc.sym.hdl_name()
                    call = AHDL_FUNCALL(AHDL_SYMBOL(rom_func_name), [input])
                    connect = AHDL_BLOCK(str(i), [AHDL_CONNECT(fname, call)])
                    case_val = '{}[{}]'.format(cs_name, i)
                    case_items.append(AHDL_CASE_ITEM(case_val, connect))
                case = AHDL_CASE(AHDL_SYMBOL('1\'b1'), case_items)
                rom_func = AHDL_FUNCTION(fname, [input, cs], [case])
            self.hdlmodule.add_function(rom_func)

    def _collect_vars(self, fsm):
        outputs = set()
        defs = set()
        uses = set()
        memnodes = set()
        collector = AHDLVarCollector(self.hdlmodule, defs, uses, outputs, memnodes)
        for stg in fsm.stgs:
            for state in stg.states:
                collector.visit(state)
        return defs, uses, outputs, memnodes

    def _collect_special_decls(self, fsm):
        edge_detectors = set()
        collector = AHDLSpecialDeclCollector(edge_detectors)
        for stg in fsm.stgs:
            for state in stg.states:
                collector.visit(state)
        return edge_detectors

    def _collect_moves(self, fsm):
        moves = []
        for stg in fsm.stgs:
            for state in stg.states:
                moves.extend([code for code in state.traverse() if code.is_a(AHDL_MOVE)])
        return moves

    def _add_single_port_interface(self, signal):
        inf = create_single_port_interface(signal)
        if inf:
            self.hdlmodule.add_interface(inf.if_name, inf)

    def _add_reset_stms(self, fsm, defs, uses, outputs):
        fsm_name = fsm.name
        for acc in self.hdlmodule.accessors.values():
            for stm in acc.reset_stms():
                self.hdlmodule.add_fsm_reset_stm(fsm_name, stm)
        # reset output ports
        for sig in outputs:
            if sig.is_net():
                continue
            infs = [inf for inf in self.hdlmodule.interfaces.values() if inf.signal is sig]
            for inf in infs:
                for stm in inf.reset_stms():
                    self.hdlmodule.add_fsm_reset_stm(fsm_name, stm)
        # reset local ports
        for sig in uses:
            if sig.is_single_port():
                local_accessors = self.hdlmodule.local_readers.values()
                accs = [acc for acc in local_accessors if acc.inf.signal is sig]
                for acc in accs:
                    for stm in acc.reset_stms():
                        self.hdlmodule.add_fsm_reset_stm(fsm_name, stm)
        for sig in defs:
            # reset internal ports
            if sig.is_single_port():
                local_accessors = self.hdlmodule.local_writers.values()
                accs = [acc for acc in local_accessors if acc.inf.signal is sig]
                for acc in accs:
                    for stm in acc.reset_stms():
                        self.hdlmodule.add_fsm_reset_stm(fsm_name, stm)
            # reset internal regs
            elif sig.is_reg():
                if sig.is_initializable():
                    v = AHDL_CONST(sig.init_value)
                else:
                    v = AHDL_CONST(0)
                mv = AHDL_MOVE(AHDL_VAR(sig, Ctx.STORE), v)
                self.hdlmodule.add_fsm_reset_stm(fsm_name, mv)

        local_readers = self.hdlmodule.local_readers.values()
        local_writers = self.hdlmodule.local_writers.values()
        accs = set(list(local_readers) + list(local_writers))
        for acc in accs:
            # reset local (SinglePort)RAM ports
            if acc.inf.signal.is_memif():
                if acc.inf.signal.sym.scope is fsm.scope:
                    for stm in acc.reset_stms():
                        self.hdlmodule.add_fsm_reset_stm(fsm_name, stm)

    def _add_mem_connections(self, scope):
        mrg = env.memref_graph
        HDLMemPortMaker(mrg.collect_ram(scope), scope, self.hdlmodule).make_port_all()
        for memnode in mrg.collect_immutable(scope):
            if memnode.is_writable():
                HDLTuplePortMaker(memnode, scope, self.hdlmodule).make_port()
        for memnode in mrg.collect_ram(scope):
            if memnode.can_be_reg():
                HDLRegArrayPortMaker(memnode, scope, self.hdlmodule).make_port()

    def _add_edge_detectors(self, fsm):
        edge_detectors = self._collect_special_decls(fsm)
        for sig, old, new in edge_detectors:
            self.hdlmodule.add_edge_detector(sig, old, new)

    def _add_sub_module_accessors(self):
        def is_acc_connected(sub, acc, hdlmodule):
            if sub_module.name == 'ram' or sub_module.name == 'fifo':
                return True
            elif sub_module.scope.is_function_module():
                return True
            elif acc.acc_name in hdlmodule.accessors:
                return True
            return False

        for name, sub_module, connections, param_map in self.hdlmodule.sub_modules.values():
            # TODO
            if sub_module.name == 'fifo':
                continue
            for conns in connections.values():
                for inf, acc in conns:
                    if not is_acc_connected(sub_module, acc, self.hdlmodule):
                        acc.connected = False
                        continue
                    tag = inf.if_name
                    for p in acc.regs():
                        int_name = acc.port_name(p)
                        sig = self.hdlmodule.gen_sig(int_name, p.width)
                        self.hdlmodule.add_internal_reg(sig, tag)
                    for p in acc.nets():
                        int_name = acc.port_name(p)
                        sig = self.hdlmodule.gen_sig(int_name, p.width)
                        self.hdlmodule.add_internal_net(sig, tag)


class HDLFunctionModuleBuilder(HDLModuleBuilder):
    def _build_module(self):
        assert len(self.hdlmodule.fsms) == 1
        fsm = self.hdlmodule.fsms[self.hdlmodule.name]
        scope = fsm.scope
        defs, uses, outputs, memnodes = self._collect_vars(fsm)
        locals = defs.union(uses)
        module_name = self.hdlmodule.name
        self._add_state_register(fsm)
        callif = CallInterface('', module_name)
        self.hdlmodule.add_interface('', callif)
        self._add_input_interfaces(scope)
        self._add_output_interfaces(scope)
        self._add_internal_ports(locals)
        self._add_mem_connections(scope)
        self._add_callee_submodules(scope)
        self._add_roms(memnodes)
        self._add_reset_stms(fsm, defs, uses, outputs)
        self._add_sub_module_accessors()

    def _add_input_interfaces(self, scope):
        if scope.is_method():
            assert False
            params = scope.params[1:]
        else:
            params = scope.params
        for i, (sym, copy, _) in enumerate(params):
            if sym.typ.is_int() or sym.typ.is_bool():
                sig_name = '{}_{}'.format(scope.base_name, sym.hdl_name())
                sig = self.hdlmodule.signal(sig_name)
                inf = SingleReadInterface(sig, sym.hdl_name(), scope.base_name)
            elif sym.typ.is_list():
                memnode = sym.typ.get_memnode()
                if memnode.can_be_reg():
                    sig = self.hdlmodule.gen_sig(memnode.name(),
                                                 memnode.data_width(),
                                                 sym=memnode.sym)
                    inf = RegArrayReadInterface(sig, memnode.name(),
                                                self.hdlmodule.name,
                                                memnode.data_width(),
                                                memnode.length)
                    self.hdlmodule.add_interface(inf.if_name, inf)
                    inf = RegArrayWriteInterface(sig, 'out_{}'.format(copy.name),
                                                 self.hdlmodule.name,
                                                 memnode.data_width(),
                                                 memnode.length)
                    self.hdlmodule.add_interface(inf.if_name, inf)
                    continue
                else:
                    sig = self.hdlmodule.gen_sig(memnode.name(),
                                                 memnode.data_width(),
                                                 sym=memnode.sym)
                    inf = RAMBridgeInterface(sig, memnode.name(),
                                             self.hdlmodule.name,
                                             memnode.data_width(),
                                             memnode.addr_width())
                    self.hdlmodule.node2if[memnode] = inf
            elif sym.typ.is_tuple():
                memnode = sym.typ.get_memnode()
                inf = TupleInterface(memnode.name(),
                                     self.hdlmodule.name,
                                     memnode.data_width(),
                                     memnode.length)
            else:
                assert False
            self.hdlmodule.add_interface(inf.if_name, inf)

    def _add_output_interfaces(self, scope):
        if scope.return_type.is_scalar():
            sig_name = '{}_out_0'.format(scope.base_name)
            sig = self.hdlmodule.signal(sig_name)
            inf = SingleWriteInterface(sig, 'out_0', scope.base_name)
            self.hdlmodule.add_interface(inf.if_name, inf)
        elif scope.return_type.is_seq():
            raise NotImplementedError('return of a suquence type is not implemented')


def accessor2module(acc):
    if isinstance(acc, FIFOWriteAccessor) or isinstance(acc, FIFOReadAccessor):
        return FIFOModule(acc.inf.signal)
    return None


class HDLTestbenchBuilder(HDLModuleBuilder):
    def _build_module(self):
        assert len(self.hdlmodule.fsms) == 1
        fsm = self.hdlmodule.fsms[self.hdlmodule.name]
        scope = fsm.scope
        defs, uses, outputs, memnodes = self._collect_vars(fsm)
        locals = defs.union(uses)
        self._add_state_register(fsm)
        self._add_internal_ports(locals)
        self._add_mem_connections(scope)
        self._add_callee_submodules(scope)
        for sym, cp, _ in scope.params:
            if sym.typ.is_object() and sym.typ.get_scope().is_module():
                mod_scope = sym.typ.get_scope()
                sub_hdlmodule = env.hdlmodule(mod_scope)
                param_map = {}
                if sub_hdlmodule.scope.module_param_vars:
                    for name, v in sub_hdlmodule.scope.module_param_vars:
                        param_map[name] = v
                self._add_submodule_instances(sub_hdlmodule, [cp.name], param_map=param_map)

        # FIXME: FIFO should be in the @module class
        for acc in self.hdlmodule.accessors.values():
            acc_mod = accessor2module(acc)
            if acc_mod:
                connections = defaultdict(list)
                for inf in acc_mod.interfaces.values():
                    inf_acc = inf.accessor(acc.inst_name)
                    if isinstance(inf, WriteInterface):
                        connections['ret'].append((inf, inf_acc))
                    else:
                        connections[''].append((inf, inf_acc))
                self.hdlmodule.add_sub_module(inf_acc.acc_name,
                                              acc_mod,
                                              connections,
                                              acc_mod.param_map)
        self._add_roms(memnodes)
        self._add_reset_stms(fsm, defs, uses, outputs)
        self._add_edge_detectors(fsm)
        self._add_sub_module_accessors()


class HDLTopModuleBuilder(HDLModuleBuilder):
    def _process_io(self, hdlmodule):
        signals = hdlmodule.get_signals()
        #signals = hdlmodule.signals
        for sig in signals.values():
            if sig.is_single_port():
                self._add_single_port_interface(sig)

    def _process_fsm(self, fsm):
        scope = fsm.scope
        defs, uses, outputs, memnodes = self._collect_vars(fsm)
        locals = defs.union(uses)
        regs, nets = self._add_internal_ports(locals)
        self._add_state_register(fsm)
        self._add_mem_connections(scope)
        self._add_callee_submodules(scope)
        self._add_roms(memnodes)
        self._add_reset_stms(fsm, defs, uses, outputs)
        self._add_edge_detectors(fsm)

    def _build_module(self):
        assert self.hdlmodule.scope.is_module()
        assert self.hdlmodule.scope.is_class()
        if not self.hdlmodule.scope.is_instantiated():
            return
        for p in self.hdlmodule.scope.module_params:
            sig = self.hdlmodule.signal(p.copy)
            assert sig
            val = 0 if not p.defval else p.defval.value
            self.hdlmodule.parameters.append((sig, val))
        self._process_io(self.hdlmodule)

        fsms = list(self.hdlmodule.fsms.values())
        for fsm in fsms:
            if fsm.scope.is_ctor():
                memnodes = []
                for sym in self.hdlmodule.scope.symbols.values():
                    if sym.typ.is_seq():
                        memnodes.append(sym.typ.get_memnode())
                self._add_roms(memnodes)

                for memnode in env.memref_graph.collect_ram(self.hdlmodule.scope):
                    assert memnode.can_be_reg()
                    name = memnode.name()
                    width = memnode.data_width()
                    length = memnode.length
                    sig = self.hdlmodule.gen_sig(name, width)
                    self.hdlmodule.add_internal_reg_array(sig, length)
                # remove ctor fsm and add constant parameter assigns
                for stm in self._collect_module_defs(fsm):
                    if stm.dst.sig.is_field():
                        if stm.dst.sig.is_reg():
                            self.hdlmodule.add_internal_reg(stm.dst.sig, '')
                        else:
                            assign = AHDL_ASSIGN(stm.dst, stm.src)
                            self.hdlmodule.add_static_assignment(assign, '')
                            self.hdlmodule.add_internal_net(stm.dst.sig, '')
                del self.hdlmodule.fsms[fsm.name]
            else:
                self._process_fsm(fsm)
        self._add_sub_module_accessors()

    def _collect_module_defs(self, fsm):
        moves = self._collect_moves(fsm)
        defs = []
        for mv in moves:
            if (mv.dst.is_a(AHDL_VAR) and
                    (mv.dst.sig.is_output() or
                     mv.dst.sig.is_field())):
                defs.append(mv)
        return defs


class AHDLVarCollector(AHDLVisitor):
    '''this class collects inputs and outputs and locals'''
    def __init__(self, hdlmodule, local_defs, local_uses, output_temps, memnodes):
        self.local_defs = local_defs
        self.local_uses = local_uses
        self.output_temps = output_temps
        self.module_constants = [c for c, _ in hdlmodule.state_constants]
        self.memnodes = memnodes

    def visit_AHDL_CONST(self, ahdl):
        pass

    def visit_AHDL_MEMVAR(self, ahdl):
        if ahdl.ctx & Ctx.STORE:
            self.local_defs.add(ahdl.sig)
        else:
            self.local_uses.add(ahdl.sig)
        self.memnodes.add(ahdl.memnode)

    def visit_AHDL_VAR(self, ahdl):
        if ahdl.sig.is_ctrl() or ahdl.sig.name in self.module_constants:
            pass
        elif ahdl.sig.is_input():
            if ahdl.sig.is_single_port():
                self.output_temps.add(ahdl.sig)
        elif ahdl.sig.is_output():
            self.output_temps.add(ahdl.sig)
        else:
            if ahdl.ctx & Ctx.STORE:
                self.local_defs.add(ahdl.sig)
            else:
                self.local_uses.add(ahdl.sig)


class AHDLSpecialDeclCollector(AHDLVisitor):
    def __init__(self, edge_detectors):
        self.edge_detectors = edge_detectors

    def visit_AHDL_META_WAIT(self, ahdl):
        if ahdl.metaid != 'WAIT_EDGE':
            return
        old, new = ahdl.args[0], ahdl.args[1]
        for var in ahdl.args[2:]:
            self.edge_detectors.add((var.sig, old, new))
