"""ViterbiK7DecoderBlock — see :class:`ViterbiK7DecoderBlock`."""
import numpy as np
from ..block import CellProgram
from typing import Dict, Optional
from ._base import KyttarBlock, BlockInterface, assemble_to_words, float_to_q15, q15_to_float


class ViterbiK7DecoderBlock(KyttarBlock):
    """
    Full K=7 Rate 1/2 Viterbi Decoder — Ring ACS Core + FPGA Traceback.

    Implements soft-decision Viterbi decoding for the CCSDS/MIL-STD K=7
    convolutional code (G1=0x79, G2=0x5B).

    Architecture: 69-Cell Ring ACS + FPGA Traceback
    =================================================

    The ACS core uses a ring topology: 64 ACS cells arranged in a
    unidirectional ring with 2 relay cells for long-distance feedback
    routing. Branch metrics are computed by 2 BMU cells and distributed
    through the ring via wavefront forwarding. A MIN cell finds the
    best-metric state each symbol and sends it to the FPGA for traceback.

    Ring Layout (66 positions):
    ```
        BMU-A → BMU-B → [ACS0] → [ACS1] → ... → [ACS21] → [RELAY-A]
        → [ACS22] → ... → [ACS42] → [RELAY-B] → [ACS43] → ... → [ACS63]
        → [MIN] → output (x1 to FPGA)
                  ↑______________________________________________________|
                  (feedback path metrics routed forward around ring)
    ```

    Cell Layout (69 cells):
    - BMU-A (1 cell): Receives LLR0/LLR1 pair, computes BM00/BM01
    - BMU-B (1 cell): Computes BM10/BM11, forwards all 4 BMs to ACS0
    - ACS0-63 (64 cells): Add-Compare-Select, 1 state per cell
    - RELAY-A (1 cell): At ring position 22, relays long-distance metrics
    - RELAY-B (1 cell): At ring position 44, relays long-distance metrics
    - MIN (1 cell): Finds minimum path metric state, outputs to FPGA

    Feedback Routing:
    - Each ACS cell sends its updated path metric to 2 successor states
    - Successors: next0 = s>>1 (input bit 0), next1 = 32|(s>>1) (input bit 1)
    - Direct routes (hop ≤ 31): 62 of 128 routes
    - 1-hop relay via intermediate ACS cell: 60 routes
    - 2-hop relay via RELAY-A then RELAY-B: 6 routes
    - Load-balanced: max 2 relay duties per ACS cell

    Memory Layout Per ACS Cell (32 words):
    - R0: Accumulator (ALU destination)
    - R1, R2: Predecessor path metrics (written by predecessor cells)
    - R3: Relay metric storage (for relay duty, if any)
    - R4-R7: Branch metrics (BM00, BM01, BM10, BM11)
    - R8: Path metric for this state
    - R9: Decision bit (0 or 1)
    - R10-R31: Program code (22 instruction slots, entry at R10)

    Modular Arithmetic:
    - Path metrics use unsigned 16-bit modular arithmetic
    - No normalization needed: CMP computes (a-b) mod 2^16, sign bit gives
      correct comparison even after wrap-around (as long as max metric spread
      < 32768, which is guaranteed by the code structure)

    FPGA Traceback Interface:
    - MIN cell sends (best_state_id, decision_bits[63:0]) via x1_out each symbol
    - FPGA stores 120 symbols of decisions (120×64 bits) in SRAM
    - FPGA performs traceback from best_state backward through stored decisions
    - FPGA outputs decoded bits via x1_in back to chip

    Interface:
        - Input: x16_in (soft bit pairs, LLR0 then LLR1)
        - Output: x1_out (best state + decisions to FPGA)
                  x1_in (decoded bits from FPGA)
    """
    CATEGORY = "fec"
    TAGS = ["viterbi", "decoder", "fec"]

    _interface = BlockInterface(entry_address=9, input_registers=[31], output_registers=[31])

    # K=7 code parameters
    K = 7
    NUM_STATES = 64
    RATE = 2
    G1 = 0x79  # Generator polynomial 1: 1+x+x^2+x^3+x^6 = 1111001
    G2 = 0x5B  # Generator polynomial 2: 1+x+x^3+x^4+x^6 = 1011011
    TRACEBACK_DEPTH = 120  # 20 * (K-1), production depth for reliable decoding

    # Ring topology constants
    RING_SIZE = 66  # 64 ACS + 2 relay cells
    RELAY_A_POS = 22
    RELAY_B_POS = 44

    # Register assignments for ACS cells (32 words total)
    # R0: accumulator (ALU output)
    R_PRED0_METRIC = 1   # Predecessor 0 path metric (written externally via feedback)
    R_PRED1_METRIC = 2   # Predecessor 1 path metric (written externally via feedback)
    R_PATH_METRIC = 3    # This state's path metric (persists across symbols)
    R_RELAY1 = 4         # Relay metric storage slot 1 (if this cell has relay duty)
    R_RELAY2 = 5         # Relay metric storage slot 2 (if 2 relay duties)
    R_BM00 = 6           # Branch metric (0,0) — received via wavefront
    R_BM01 = 7           # Branch metric (0,1) — received via wavefront
    # BM10 = -BM01 and BM11 = -BM00 are NOT stored; inline SUB used instead
    R_CAND0 = 8          # Temporary: candidate 0 metric during ACS
    R_ENTRY = 9          # Program entry address (R9-R31 = 23 instruction slots)

    def __init__(self, name: str):
        super().__init__(name)
        self._build_trellis()
        self._build_ring_routing()

    def _build_trellis(self):
        """Pre-compute the K=7 trellis structure."""
        self._prev_states = np.zeros((self.NUM_STATES, 2), dtype=np.int32)
        self._prev_inputs = np.zeros((self.NUM_STATES, 2), dtype=np.int32)
        self._bm_indices = np.zeros((self.NUM_STATES, 2), dtype=np.int32)

        for state in range(self.NUM_STATES):
            # Predecessors: pred0 = 2*(s%32), pred1 = 2*(s%32)+1
            pred0 = (state & 0x1F) << 1
            pred1 = pred0 | 1
            self._prev_states[state, 0] = pred0
            self._prev_states[state, 1] = pred1

            # Input bit for this transition (same for both preds)
            inp_bit = (state >> 5) & 1
            self._prev_inputs[state, 0] = inp_bit
            self._prev_inputs[state, 1] = inp_bit

            # Expected encoder outputs → correlation BM index
            # 7-bit shift register = (inp_bit << 6) | prev_state
            # BM index selects which correlation BM to use.
            # ACS cells use SUB (pred - BM_corr) to compute distance metric.
            for path_idx, prev in enumerate([pred0, pred1]):
                sr = (inp_bit << 6) | prev
                out0 = self._parity(sr & self.G1)
                out1 = self._parity(sr & self.G2)
                self._bm_indices[state, path_idx] = out0 * 2 + out1

    @staticmethod
    def _parity(x: int) -> int:
        """Compute parity (XOR of all bits)."""
        x ^= x >> 16
        x ^= x >> 8
        x ^= x >> 4
        x ^= x >> 2
        x ^= x >> 1
        return x & 1

    @staticmethod
    def _state_to_ring(s: int) -> int:
        """Map trellis state (0-63) to ring position (0-65)."""
        if s < 22:
            return s
        elif s < 43:
            return s + 1   # skip relay A at position 22
        else:
            return s + 2   # skip relays A and B at positions 22, 44

    @staticmethod
    def _ring_to_state(p: int) -> Optional[int]:
        """Map ring position (0-65) to trellis state. Returns None for relay positions."""
        if p < 22:
            return p
        elif p == 22:
            return None  # relay A
        elif p < 44:
            return p - 1
        elif p == 44:
            return None  # relay B
        else:
            return p - 2

    def _get_target_reg(self, src_state: int, tgt_state: int) -> int:
        """Determine which register in target cell the source writes to.
        Returns R1 (pred0 slot) or R2 (pred1 slot)."""
        tgt_pred0 = (tgt_state & 0x1F) << 1
        return self.R_PRED0_METRIC if tgt_pred0 == src_state else self.R_PRED1_METRIC

    def _build_ring_routing(self):
        """Compute load-balanced feedback routing for all 128 trellis edges."""
        # 2-hop routes: states 1-6 sending to states 0-3 via both relays
        self._two_hop_routes = {}
        two_hop_pairs = [
            (1, 0, 21), (2, 1, 20), (3, 1, 19),
            (4, 2, 18), (5, 2, 17), (6, 3, 16),
        ]
        for src, tgt, hop_to_relA in two_hop_pairs:
            treg = self._get_target_reg(src, tgt)
            p_tgt = self._state_to_ring(tgt)
            hop_B_to_tgt = (p_tgt - self.RELAY_B_POS) % self.RING_SIZE
            self._two_hop_routes[src] = {
                'target': tgt, 'hop_to_relA': hop_to_relA,
                'hop_A_to_B': 22, 'hop_B_to_tgt': hop_B_to_tgt,
                'treg': treg,
            }

        # Collect 1-hop relay routes
        need_relay = []
        for s in range(self.NUM_STATES):
            n0 = s >> 1
            n1 = 32 | (s >> 1)
            p_s = self._state_to_ring(s)
            for target in [n0, n1]:
                p_t = self._state_to_ring(target)
                dist = (p_t - p_s) % self.RING_SIZE
                if dist <= 31:
                    continue
                if s in self._two_hop_routes and target == self._two_hop_routes[s]['target']:
                    continue
                need_relay.append((s, target, p_s, p_t, dist))

        need_relay.sort(key=lambda x: -x[4])

        # Load-balanced assignment of relay duties
        self._relay_duties = {s: [] for s in range(self.NUM_STATES)}

        for src, tgt, p_s, p_t, dist in need_relay:
            cands = []
            for mid in range(self.RING_SIZE):
                if mid == p_s or mid == p_t:
                    continue
                d1 = (mid - p_s) % self.RING_SIZE
                d2 = (p_t - mid) % self.RING_SIZE
                if 0 < d1 <= 31 and 0 < d2 <= 31:
                    ms = self._ring_to_state(mid)
                    if ms is not None:
                        load = len(self._relay_duties[ms])
                        cands.append((mid, ms, d1, d2, load))

            cands.sort(key=lambda x: x[4])
            if cands:
                mid, ms, d1, d2, _ = cands[0]
                treg = self._get_target_reg(src, tgt)
                self._relay_duties[ms].append({
                    'from': src, 'to': tgt, 'hop': d2, 'treg': treg,
                })

        # Build per-cell successor routing table
        self._succ_routes = {}
        for s in range(self.NUM_STATES):
            n0 = s >> 1
            n1 = 32 | (s >> 1)
            p_s = self._state_to_ring(s)
            routes = []
            for target in [n0, n1]:
                p_t = self._state_to_ring(target)
                dist = (p_t - p_s) % self.RING_SIZE
                treg = self._get_target_reg(s, target)

                if dist <= 31:
                    routes.append({'target': target, 'hop': dist, 'treg': treg, 'via': None})
                elif s in self._two_hop_routes and target == self._two_hop_routes[s]['target']:
                    hop_to_relA = self._two_hop_routes[s]['hop_to_relA']
                    routes.append({'target': target, 'hop': hop_to_relA,
                                   'treg': treg, 'via': '2hop_relay'})
                else:
                    # Find the assigned intermediate
                    for ms in range(self.NUM_STATES):
                        for d in self._relay_duties[ms]:
                            if d['from'] == s and d['to'] == target:
                                p_mid = self._state_to_ring(ms)
                                hop_to_mid = (p_mid - p_s) % self.RING_SIZE
                                routes.append({'target': target, 'hop': hop_to_mid,
                                               'treg': treg, 'via': ms})
                                break
                        else:
                            continue
                        break
            self._succ_routes[s] = routes

    @property
    def cell_count(self) -> int:
        # 2 BMU + 64 ACS + 2 relay + 1 MIN = 69 cells
        return 69

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def _build_bmu_a_program(self) -> CellProgram:
        """BMU-A: receives 2 soft bits sequentially, computes BM00/BM01, forwards to BMU-B."""
        prog = CellProgram()

        # R1: LLR0, R2: LLR1, R3: BM00, R4: BM01
        # R5: zero, R6: counter (0=wait LLR0, 1=wait LLR1), R7: one
        prog.set_memory(5, 0)
        prog.set_memory(6, 0)
        prog.set_memory(7, 1)

        assembly = """; BMU-A: receives LLR pair, computes BM00/BM01, sends to BMU-B
start:
    CMP R6, R5
    BR.NZ have_llr0
    MOVE R1, R31
    MOVE R6, R7
    HALT
have_llr0:
    MOVE R2, R31
    MOVE R6, R5
    ADD R1, R2
    MOVE R3, R0
    SUB R1, R2
    MOVE R4, R0
    MOVE R0, R3
    WRITE @1, 1
    MOVE R0, R4
    WRITE @1, 2
    JUMP @1, 9
    HALT
"""
        words = assemble_to_words(assembly, base_addr=9)
        prog.set_program(9, words)
        return prog

    def _build_bmu_b_program(self) -> CellProgram:
        """BMU-B: receives BM00/BM01, sends both to ACS0 and triggers wavefront."""
        prog = CellProgram()

        # R1: BM00 (from BMU-A), R2: BM01
        # BMU-B just forwards BM00/BM01 to ACS0. ACS cells compute BM10/BM11 inline.
        assembly = f"""; BMU-B: forwards BM00/BM01 to ACS0, triggers wavefront
start:
    MOVE R0, R1
    WRITE @1, {self.R_BM00}
    MOVE R0, R2
    WRITE @1, {self.R_BM01}
    JUMP @1, {self.R_ENTRY}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=9)
        prog.set_program(9, words)
        return prog

    def _bm_instruction(self, pred_reg: int, bm_idx: int) -> str:
        """Generate the SUB instruction to compute distance metric.

        Distance = pred_metric - BM_corr[idx] (minimization).
        BM indices: 0=BM00(R6), 1=BM01(R7), 2=BM10(-BM01), 3=BM11(-BM00).
        BM00 in R6, BM01 in R7. BM10/BM11 not stored — use ADD (double negation).
        SUB R_pred, R_BM = pred - corr = distance (for idx 0,1).
        ADD R_pred, R_BM = pred + BM = pred - (-BM) = distance (for idx 2,3 where BM=-corr).
        """
        if bm_idx == 0:
            return f"    SUB R{pred_reg}, R{self.R_BM00}"
        elif bm_idx == 1:
            return f"    SUB R{pred_reg}, R{self.R_BM01}"
        elif bm_idx == 2:
            # BM10 = -BM01, so dist = pred - BM10 = pred + BM01
            return f"    ADD R{pred_reg}, R{self.R_BM01}"
        else:  # bm_idx == 3
            # BM11 = -BM00, so dist = pred - BM11 = pred + BM00
            return f"    ADD R{pred_reg}, R{self.R_BM00}"

    def _build_acs_program(self, state: int) -> CellProgram:
        """
        Build ACS cell program for one trellis state.

        Instruction budget (23 slots, R9-R31):
        - ACS core: 7 instructions (ADD/SUB, MOVE, ADD/SUB, CMP, BR, MOVE, MOVE)
        - Successor metric WRITEs: 3 (MOVE R0 + 2x WRITE)
        - BM wavefront forwarding: 5 (2x MOVE+WRITE for BM00/BM01 + JUMP)
        - Relay duties: 2 per duty (MOVE + WRITE), max 2 = 4
        - HALT: 1
        - Total max: 7 + 3 + 5 + 4 + 1 = 20 (3 spare)
        """
        prog = CellProgram()

        # Initialize path metric: state 0 = 0, others = 0x7FFF
        prog.set_memory(self.R_PATH_METRIC, 0 if state == 0 else 0x7FFF)

        bm0_idx = int(self._bm_indices[state, 0])
        bm1_idx = int(self._bm_indices[state, 1])
        pred0 = int(self._prev_states[state, 0])
        pred1 = int(self._prev_states[state, 1])

        succ = self._succ_routes[state]
        relay = self._relay_duties[state]

        # Wavefront hop to next cell in chain.
        # States 21 and 42 send to relay cells (1 hop), which then forward to next ACS.
        # All other states send directly to next ACS (1 hop).
        wf_hop = 1

        lines = []
        lines.append(f"; ACS State {state}: preds [{pred0},{pred1}], bm_idx [{bm0_idx},{bm1_idx}]")
        lines.append("")
        lines.append("start:")

        # === ACS CORE (7 instructions) ===
        # cand0 = pred0_metric + BM[bm0] -> R8
        lines.append(self._bm_instruction(self.R_PRED0_METRIC, bm0_idx))
        lines.append(f"    MOVE R{self.R_CAND0}, R0")

        # cand1 = pred1_metric + BM[bm1] -> stays in R0
        lines.append(self._bm_instruction(self.R_PRED1_METRIC, bm1_idx))

        # Compare cand0 vs cand1 (minimize distance metric)
        # CMP R8, R0 → flags reflect (cand0 - cand1), R0 still holds cand1
        # If cand0 < cand1 (N set), select cand0
        lines.append(f"    CMP R{self.R_CAND0}, R0")
        lines.append(f"    BR.N sel0")
        # cand1 wins (cand0 >= cand1): R0 = cand1
        lines.append(f"    MOVE R{self.R_PATH_METRIC}, R0")
        lines.append(f"    GOTO fwd")
        lines.append(f"sel0:")
        lines.append(f"    MOVE R{self.R_PATH_METRIC}, R{self.R_CAND0}")

        lines.append(f"fwd:")

        # === SUCCESSOR METRIC WRITES (3 instructions) ===
        # Both successors receive the same metric value (our new path metric)
        lines.append(f"    MOVE R0, R{self.R_PATH_METRIC}")
        for route in succ:
            tgt = route['target']
            hop = route['hop']
            treg = route['treg']
            via = route['via']

            if via is None:
                # Direct write to successor
                lines.append(f"    WRITE @{hop}, {treg}")
            elif via == '2hop_relay':
                # Write to relay A's storage (relay will forward to B, then to target)
                # Use target register index (1-6) in relay A to identify this route
                two_hop = self._two_hop_routes[state]
                relay_a_reg = self._two_hop_relay_a_slot(state)
                lines.append(f"    WRITE @{hop}, {relay_a_reg}")
            else:
                # Write to intermediate ACS cell's relay register
                p_s = self._state_to_ring(state)
                p_mid = self._state_to_ring(via)
                hop_to_mid = (p_mid - p_s) % self.RING_SIZE
                relay_slot = self._get_relay_slot(via, state, tgt)
                lines.append(f"    WRITE @{hop_to_mid}, {relay_slot}")

        # === BM WAVEFRONT FORWARDING (5 instructions, or 3 for state 63) ===
        if state < 63:
            # States 21 and 42 forward to relay cells which use R7/R8 for BMs
            if state in (21, 42):
                bm_dst0, bm_dst1 = 7, 8
            else:
                bm_dst0, bm_dst1 = self.R_BM00, self.R_BM01
            lines.append(f"    MOVE R0, R{self.R_BM00}")
            lines.append(f"    WRITE @{wf_hop}, {bm_dst0}")
            lines.append(f"    MOVE R0, R{self.R_BM01}")
            lines.append(f"    WRITE @{wf_hop}, {bm_dst1}")
            lines.append(f"    JUMP @{wf_hop}, {self.R_ENTRY}")
        else:
            # State 63: send metric to MIN cell (1 hop away)
            lines.append(f"    MOVE R0, R{self.R_PATH_METRIC}")
            lines.append(f"    WRITE @1, 1")
            lines.append(f"    JUMP @1, 9")

        # === RELAY DUTIES (2 per duty, max 4 instructions) ===
        for i, duty in enumerate(relay):
            relay_reg = self.R_RELAY1 + i
            hop = duty['hop']
            treg = duty['treg']
            lines.append(f"    MOVE R0, R{relay_reg}")
            lines.append(f"    WRITE @{hop}, {treg}")

        lines.append(f"    HALT")

        assembly = "\n".join(lines)
        words = assemble_to_words(assembly, base_addr=self.R_ENTRY)
        prog.set_program(self.R_ENTRY, words)
        return prog

    def _two_hop_relay_a_slot(self, src_state: int) -> int:
        """Map a 2-hop source state to relay A's register slot (R1-R6)."""
        # States 1-6 map to relay A slots R1-R6
        slot_map = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        return slot_map[src_state]

    def _get_relay_slot(self, intermediate_state: int, src_state: int, tgt_state: int) -> int:
        """Get the relay register in an intermediate ACS cell for a given route."""
        duties = self._relay_duties[intermediate_state]
        for i, d in enumerate(duties):
            if d['from'] == src_state and d['to'] == tgt_state:
                return self.R_RELAY1 + i
        raise ValueError(f"No relay duty found for {src_state}->{tgt_state} via {intermediate_state}")

    def _build_relay_a_program(self) -> CellProgram:
        """
        RELAY-A at ring position 22.

        Dedicated relay cell for 2-hop feedback routes. States 1-6 WRITE their
        path metrics to R1-R6 here. When the wavefront JUMP arrives from ACS21,
        RELAY-A forwards all 6 metrics to RELAY-B (22 hops away), then passes
        the wavefront BMs through to ACS22 (1 hop away).

        Route mapping (state -> relay_A register):
        State 1 -> R1, State 2 -> R2, State 3 -> R3,
        State 4 -> R4, State 5 -> R5, State 6 -> R6
        """
        prog = CellProgram()

        # R1-R6: relay storage (written by states 1-6)
        # R7-R8: BM00/BM01 from wavefront (received from ACS21)
        # Relay doesn't do ACS — 23 instruction slots is plenty.

        assembly = f"""; RELAY-A: 2-hop relay + wavefront passthrough (ring pos 22)
; R1-R6: relay metrics from states 1-6
; R7,R8: BM00/BM01 from wavefront

start:
    ; Forward 6 relay metrics to RELAY-B (22 hops away)
    MOVE R0, R1
    WRITE @22, 1
    MOVE R0, R2
    WRITE @22, 2
    MOVE R0, R3
    WRITE @22, 3
    MOVE R0, R4
    WRITE @22, 4
    MOVE R0, R5
    WRITE @22, 5
    MOVE R0, R6
    WRITE @22, 6
    ; Pass wavefront BMs to ACS22 (1 hop) and trigger
    MOVE R0, R7
    WRITE @1, {self.R_BM00}
    MOVE R0, R8
    WRITE @1, {self.R_BM01}
    JUMP @1, {self.R_ENTRY}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=self.R_ENTRY)
        prog.set_program(self.R_ENTRY, words)
        return prog

    def _build_relay_b_program(self) -> CellProgram:
        """
        RELAY-B at ring position 44.

        Final hop for 2-hop routes. Receives 6 metrics from RELAY-A and
        delivers each to its target state's predecessor register. Also
        passes wavefront BMs through to ACS43.

        Delivery mapping (relay_B register -> target):
        R1 -> state 0, R2 (hop 22) | R2 -> state 1, R1 (hop 23)
        R3 -> state 1, R2 (hop 23) | R4 -> state 2, R1 (hop 24)
        R5 -> state 2, R2 (hop 24) | R6 -> state 3, R1 (hop 25)
        """
        prog = CellProgram()

        assembly = f"""; RELAY-B: 2-hop final delivery + wavefront passthrough (ring pos 44)
; R1-R6: relay metrics from RELAY-A, R7-R8: BMs from wavefront

start:
    ; Deliver metrics to target states
    MOVE R0, R1
    WRITE @22, {self.R_PRED1_METRIC}
    MOVE R0, R2
    WRITE @23, {self.R_PRED0_METRIC}
    MOVE R0, R3
    WRITE @23, {self.R_PRED1_METRIC}
    MOVE R0, R4
    WRITE @24, {self.R_PRED0_METRIC}
    MOVE R0, R5
    WRITE @24, {self.R_PRED1_METRIC}
    MOVE R0, R6
    WRITE @25, {self.R_PRED0_METRIC}
    ; Pass wavefront BMs to ACS43 (1 hop) and trigger
    MOVE R0, R7
    WRITE @1, {self.R_BM00}
    MOVE R0, R8
    WRITE @1, {self.R_BM01}
    JUMP @1, {self.R_ENTRY}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=self.R_ENTRY)
        prog.set_program(self.R_ENTRY, words)
        return prog

    def _build_min_program(self, output_hop: int,
                           target_interface: BlockInterface) -> CellProgram:
        """
        MIN cell: receives metric from ACS63, outputs best state to FPGA.

        In production, the FPGA runs its own parallel Viterbi decoder on
        the same soft bits. The on-chip MIN cell sends the final metric
        from the wavefront to signal "symbol complete" to the FPGA.
        The FPGA uses its own traceback to produce decoded bits.

        For testing: MIN cell just passes through the received metric value.
        """
        prog = CellProgram()

        target_input = target_interface.input_registers[0]
        target_entry = target_interface.entry_address

        # R1: metric from ACS63 (written by WRITE @1, 1 from ACS63)
        assembly = f"""; MIN Cell: signals symbol completion to downstream
; R1: metric from ACS63

start:
    MOVE R0, R1
    WRITE @{output_hop}, {target_input}
    JUMP @{output_hop}, {target_entry}
    HALT
"""
        words = assemble_to_words(assembly, base_addr=9)
        prog.set_program(9, words)
        return prog

    def build_cell_programs(self) -> Dict[int, CellProgram]:
        """
        V2 declarative cell programs for the Viterbi K=7 ring decoder.

        The ring ACS architecture uses per-state assembly with computed
        constants (BM register indices, hop counts, relay routing) that
        are different for each cell, so we use build_cell_programs() with
        the Rust assembler directly rather than templates.
        """
        # Delegate to v1 — the per-cell program generation is inherently
        # imperative due to 64 unique trellis configurations
        return self.build_cell_programs()

    def process_reference(self, soft_bits: np.ndarray) -> np.ndarray:
        """
        Reference implementation of K=7 Viterbi decoding (Q15 fixed-point).

        Matches the hardware implementation exactly: modular 16-bit arithmetic,
        same branch metric polarity, same trellis structure.

        Args:
            soft_bits: Input LLRs as float, shape (N*2,) for rate 1/2.
                       Positive = more likely 0, Negative = more likely 1.

        Returns:
            decoded_bits: Decoded data bits as int array
        """
        n_symbols = len(soft_bits) // self.RATE
        if n_symbols == 0:
            return np.array([], dtype=np.int32)

        # Path metrics (signed Python ints, minimize distance)
        path_metric = [0x7FFFFFFF] * self.NUM_STATES
        path_metric[0] = 0

        # Survivor paths: for each (time, state), which predecessor path was chosen
        survivors = np.zeros((n_symbols, self.NUM_STATES), dtype=np.int32)

        for t in range(n_symbols):
            llr0 = soft_bits[t * 2]
            llr1 = soft_bits[t * 2 + 1]

            # Q15: signed 16-bit integer in range [-32768, 32767]
            llr0_q15 = int(round(float(llr0) * 32768.0))
            llr1_q15 = int(round(float(llr1) * 32768.0))
            llr0_q15 = max(-32768, min(32767, llr0_q15))
            llr1_q15 = max(-32768, min(32767, llr1_q15))

            # Correlation branch metrics (signed Python ints)
            bm_corr = [
                llr0_q15 + llr1_q15,    # BM00 = corr with (0,0)
                llr0_q15 - llr1_q15,    # BM01 = corr with (0,1)
                -llr0_q15 + llr1_q15,   # BM10 = corr with (1,0)
                -llr0_q15 - llr1_q15,   # BM11 = corr with (1,1)
            ]

            new_metric = [0x7FFFFFFF] * self.NUM_STATES

            for state in range(self.NUM_STATES):
                for path_idx in range(2):
                    prev_state = int(self._prev_states[state, path_idx])
                    bm_idx = int(self._bm_indices[state, path_idx])

                    # Distance = pred_metric - correlation (minimize)
                    cand = path_metric[prev_state] - bm_corr[bm_idx]

                    if cand < new_metric[state]:
                        new_metric[state] = cand
                        survivors[t, state] = path_idx

            path_metric = new_metric

            path_metric = new_metric

        # Traceback from best final state (minimum distance)
        best_state = min(range(self.NUM_STATES), key=lambda s: path_metric[s])
        decoded = np.zeros(n_symbols, dtype=np.int32)
        state = best_state

        for t in range(n_symbols - 1, -1, -1):
            path_idx = survivors[t, state]
            inp = int(self._prev_inputs[state, path_idx])
            decoded[t] = inp
            state = int(self._prev_states[state, path_idx])

        return decoded

    def reset(self):
        """Reset Viterbi decoder state."""
        pass
