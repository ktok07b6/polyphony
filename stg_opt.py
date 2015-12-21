﻿from collections import defaultdict
from ahdl import AHDL_CONST, AHDL_VAR, AHDL_MOVE, AHDL_IF, AHDL_META
from logging import getLogger
logger = getLogger(__name__)

class STGOptimizer():
    def __init__(self):
        self.stg_return_state = {}

    def process(self, module):
        self.module = module
        self._concat_stgs()

        #usedef = STGUseDefDetector()
        #usedef.process(module)
        #usedef.table.dump()

        # all_vars = usedef.table.get_all_vars()
        # for var in all_vars:
        #     if var.sym.ancestor:
        #         if not var.sym.is_condition():
        #             logger.debug('back to ancestor : ' + str(var.sym) + ' = ' + str(var.sym.ancestor))
        #             var.sym = var.sym.ancestor

        self._remove_move()

        for stg in module.stgs:
            logger.debug(str(stg))

        self._remove_empty_state()

    def _concat_stgs(self):
        for stg in self.module.stgs:
            for i, state in enumerate(stg.states()):
                self._process_concat_state(state)

    def _process_concat_state(self, state):
        remove_codes = []
        for code in state.codes:
            if isinstance(code, AHDL_META):
                if code.metaid == 'STG_JUMP':
                    stg_name = code.args[0]
                    stg = self.module.find_stg(stg_name)
                    target_state = stg.init_state
                    _, ret_state, _ = state.next_states[0]
                    self.stg_return_state[stg_name] = ret_state
                    
                    state.next_states = []
                    state.set_next((AHDL_CONST(1), target_state, None))
                    remove_codes.append(code)
                elif code.metaid == 'STG_EXIT':
                    top = self.module.stgs[0]
                    state.next_states = []
                    state.set_next((AHDL_CONST(1), top.finish_state, None))
        for code in remove_codes:
            state.codes.remove(code)

        #add the state transition to a state in the other stg
        stg = state.stg
        assert state.next_states
        cond1, nstate1, _ = state.next_states[0]
        if cond1 is None or isinstance(cond1, AHDL_CONST):
            if state is stg.finish_state and stg.name in self.stg_return_state:
                ret_state = self.stg_return_state[stg.name]
                #replace return state
                state.next_states = []
                state.set_next((AHDL_CONST(1), ret_state, None))
                

    def _remove_move(self):
        for stg in self.module.stgs:
            for state in stg.states():
                self._process_remove_move_state(state)

    def _process_remove_move_state(self, state):
        remove_mv = []
        for code in state.codes:
            if isinstance(code, AHDL_MOVE):
                mv = code
                if isinstance(mv.src, AHDL_VAR) and isinstance(mv.dst, AHDL_VAR):
                    if mv.src.sym is mv.dst.sym:
                        remove_mv.append(mv)
        for mv in remove_mv:
            state.codes.remove(mv)


    def _remove_empty_state(self):
        for stg in self.module.stgs:
            empty_states = []
            for state in stg.states():
                if not state.codes:
                    cond, _, codes = state.next_states[0]
                    if cond is None or isinstance(cond, AHDL_CONST):
                        self.disconnect_state(state)
                        empty_states.append(state)
            for s in empty_states:
                stg.remove_state(s)

    def disconnect_state(self, state):
        _, nstate, _ = state.next_states[0]
        nstate.prev_states.remove(state)

        for prev in state.prev_states:
            prev.replace_next(state, nstate)
            nstate.set_prev(prev)

class STGUseDefTable:
    def __init__(self):
        self._sym_defs_stm = defaultdict(set)
        self._sym_uses_stm = defaultdict(set)
        self._var_defs_stm = defaultdict(set)
        self._var_uses_stm = defaultdict(set)

    def add_var_def(self, var, stm):
        self._sym_defs_stm[var.sym].add(stm)
        self._var_defs_stm[var].add(stm)

    def remove_var_def(self, var, stm):
        self._sym_defs_stm[var.sym].discard(stm)
        self._var_defs_stm[var].discard(stm)

    def add_var_use(self, var, stm):
        self._sym_uses_stm[var.sym].add(stm)
        self._var_uses_stm[var].add(stm)

    def remove_var_use(self, var, stm):
        self._sym_uses_stm[var.sym].discard(stm)
        self._var_uses_stm[var].discard(stm)

    def get_all_vars(self):
        def_stm = self._var_defs_stm
        vs = list(def_stm.keys())
        use_stm = self._var_uses_stm
        vs.extend(use_stm.keys())
        return vs

    def dump(self):
        logger.debug('statements that has symbol defs')
        for sym, stms in self._sym_defs_stm.items():
            logger.debug(sym)
            for stm in stms:
                logger.debug('    ' + str(stm))

        logger.debug('statements that has symbol uses')
        for sym, stms in self._sym_uses_stm.items():
            logger.debug(sym)
            for stm in stms:
                logger.debug('    ' + str(stm))


class STGUseDefDetector():
    def __init__(self):
        super().__init__()
        self.table = STGUseDefTable()

    def process(self, module):
        for stg in module.stgs:
            for i, state in enumerate(stg.states()):
                self._process_State(state)

    def _process_State(self, state):
        for code in state.codes:
            self.current_stm = code
            self.visit(code)

        for _, _, codes in state.next_states:
            if codes:
                for code in codes:
                    self.current_stm = code
                    self.visit(code)

    def visit_AHDL_CONST(self, ahdl):
        pass

    def visit_AHDL_VAR(self, ahdl):
        pass

    def visit_AHDL_OP(self, ahdl):
        if isinstance(ahdl.left, AHDL_VAR):
            self.table.add_var_use(ahdl.left, self.current_stm)
        else:
            self.visit(ahdl.left)

        if ahdl.right:
            if isinstance(ahdl.right, AHDL_VAR):
                self.table.add_var_use(ahdl.right, self.current_stm)
            else:
                self.visit(ahdl.right)
            
    def visit_AHDL_MEM(self, ahdl):
        pass

    def visit_AHDL_NOP(self, ahdl):
        pass

    def visit_AHDL_MOVE(self, ahdl):
        if isinstance(ahdl.src, AHDL_VAR):
            self.table.add_var_use(ahdl.src, self.current_stm)
        else:
            self.visit(ahdl.src)

        if isinstance(ahdl.dst, AHDL_VAR):
            self.table.add_var_def(ahdl.dst, self.current_stm)
        else:
            self.visit(ahdl.dst)

    def visit_AHDL_STORE(self, ahdl):
        if isinstance(ahdl.src, AHDL_VAR):
            self.table.add_var_use(ahdl.src, self.current_stm)
        else:
            self.visit(ahdl.src)

    def visit_AHDL_LOAD(self, ahdl):
        if isinstance(ahdl.dst, AHDL_VAR):
            self.table.add_var_def(ahdl.dst, self.current_stm)
        else:
            self.visit(ahdl.dst)

    def visit_AHDL_IF(self, ahdl):
        
        for cond, codes in zip(ahdl.conds, ahdl.codes_list):
            self.current_stm = ahdl
            if isinstance(cond, AHDL_VAR):
                self.table.add_var_use(cond, ahdl)
            else:
                self.visit(cond)

            for code in codes:
                self.current_stm = code
                self.visit(code)

    def visit_AHDL_FUNCALL(self, ahdl):
        for arg in ahdl.args:
            if isinstance(arg, AHDL_VAR):
                self.table.add_var_use(arg, self.current_stm)
            else:
                self.visit(arg)


    def visit_AHDL_PROCCALL(self, ahdl):
        for arg in ahdl.args:
            if isinstance(arg, AHDL_VAR):
                self.table.add_var_use(arg, self.current_stm)
            else:
                self.visit(arg)

    def visit_AHDL_META(self, ahdl):
        pass

    def visit(self, ahdl):
        method = 'visit_' + ahdl.__class__.__name__
        visitor = getattr(self, method, None)
        return visitor(ahdl)



