# Kyttar prototype — 7 SDR transceivers on one async chip

Seven complete software-defined-radio transceivers — four digital modems and three
analog transceivers — each placed, routed, and running on **one 120-cell (10×12)
Kyttar asynchronous array**. Simplex, saturated: sink (output) sample rate and the
power drawn, per direction.

**DIGITAL MODEMS** (recovered at BER 0)

| Design | RX rate | RX power | TX rate | TX power |
|--------|--------:|--------:|--------:|--------:|
| BPSK   | 188 kSa/s | 9.6 mW  | 481 kSa/s | 7.6 mW  |
| QPSK   | 172 kSa/s | 8.2 mW  | 460 kSa/s | 9.1 mW  |
| 4FSK   | 542 kSa/s | 15.2 mW | 225 kSa/s | 4.2 mW  |
| 16-QAM | 146 kSa/s | 8.9 mW  | 460 kSa/s | 11.5 mW |

**ANALOG TRANSCEIVERS** (audio correlation vs input)

| Design | RX rate | RX power | TX rate | TX power | corr  |
|--------|--------:|--------:|--------:|--------:|------:|
| AM     | 460 kSa/s  | 10.1 mW | 479 kSa/s | 5.9 mW  | 0.998 |
| FM     | 1.93 MSa/s | 6.3 mW  | 429 kSa/s | 5.7 mW  | 0.996 |
| SSB    | 346 kSa/s  | 14.0 mW | 346 kSa/s | 14.4 mW | 0.97  |

Power is total draw (active + idle) during that direction — asynchronous, so only the
cells doing work burn energy (**idle ~0.4–0.6 mW** across all designs). RX and TX differ
because each lights up a different set of cells. FM's receive path (a bare quadrature
discriminator, no feedback loops) is the fastest demod at **1.93 MSa/s** on just 3
active cells.
