"""
Intel HEX format reader/writer for Kyttar bitstreams.

The Intel HEX format is a standard ASCII text format for representing binary data.
Each line (record) has the format:
    :LLAAAA00DDDD...DDCC

Where:
    : = Start code
    LL = Byte count (number of data bytes)
    AAAA = Address (16-bit, we use 0000)
    00 = Record type (00 = data, 01 = EOF)
    DD = Data bytes (2 hex chars per byte)
    CC = Checksum (two's complement of sum of all bytes)

For Kyttar, we store 16-bit words in big-endian format (high byte first).
"""

from typing import List, BinaryIO, TextIO
from pathlib import Path


class IntelHexWriter:
    """Write Intel HEX format files."""

    def __init__(self, bytes_per_line: int = 16):
        """
        Initialize writer.

        Args:
            bytes_per_line: Number of data bytes per record (default 16 = 8 words)
        """
        self.bytes_per_line = bytes_per_line

    def _calculate_checksum(self, data: bytes) -> int:
        """Calculate Intel HEX checksum (two's complement of sum)."""
        return (-(sum(data) & 0xFF)) & 0xFF

    def _format_record(self, record_type: int, address: int, data: bytes) -> str:
        """Format a single Intel HEX record."""
        byte_count = len(data)
        addr_hi = (address >> 8) & 0xFF
        addr_lo = address & 0xFF

        # Build byte sequence for checksum
        record_bytes = bytes([byte_count, addr_hi, addr_lo, record_type]) + data
        checksum = self._calculate_checksum(record_bytes)

        # Format as hex string
        data_hex = data.hex().upper()
        return f":{byte_count:02X}{address:04X}{record_type:02X}{data_hex}{checksum:02X}"

    def write(self, words: List[int], output: TextIO):
        """
        Write 16-bit words to Intel HEX format.

        Args:
            words: List of 16-bit words to write
            output: Text file object to write to
        """
        # Convert words to bytes (big-endian)
        data_bytes = bytearray()
        for word in words:
            data_bytes.append((word >> 8) & 0xFF)  # High byte
            data_bytes.append(word & 0xFF)         # Low byte

        # Write data records
        offset = 0
        while offset < len(data_bytes):
            chunk = data_bytes[offset:offset + self.bytes_per_line]
            record = self._format_record(0x00, 0x0000, bytes(chunk))
            output.write(record + "\n")
            offset += self.bytes_per_line

        # Write EOF record
        eof_record = self._format_record(0x01, 0x0000, b"")
        output.write(eof_record + "\n")

    def write_file(self, words: List[int], path: str):
        """Write words to a file."""
        with open(path, 'w') as f:
            self.write(words, f)


class IntelHexReader:
    """Read Intel HEX format files."""

    def _parse_record(self, line: str) -> tuple:
        """
        Parse a single Intel HEX record.

        Returns:
            (record_type, address, data_bytes) or None if invalid
        """
        line = line.strip()
        if not line or line[0] != ':':
            return None

        try:
            byte_count = int(line[1:3], 16)
            address = int(line[3:7], 16)
            record_type = int(line[7:9], 16)

            data_hex = line[9:9 + byte_count * 2]
            data = bytes.fromhex(data_hex)

            checksum = int(line[9 + byte_count * 2:9 + byte_count * 2 + 2], 16)

            # Verify checksum
            record_bytes = bytes([byte_count, (address >> 8) & 0xFF,
                                  address & 0xFF, record_type]) + data
            expected_checksum = (-(sum(record_bytes) & 0xFF)) & 0xFF
            if checksum != expected_checksum:
                raise ValueError(f"Checksum mismatch: got {checksum:02X}, expected {expected_checksum:02X}")

            return (record_type, address, data)

        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid Intel HEX record: {line}") from e

    def read(self, input_file: TextIO) -> List[int]:
        """
        Read 16-bit words from Intel HEX format.

        Args:
            input_file: Text file object to read from

        Returns:
            List of 16-bit words
        """
        data_bytes = bytearray()

        for line in input_file:
            result = self._parse_record(line)
            if result is None:
                continue

            record_type, address, data = result

            if record_type == 0x00:  # Data record
                data_bytes.extend(data)
            elif record_type == 0x01:  # EOF record
                break

        # Convert bytes to words (big-endian)
        words = []
        for i in range(0, len(data_bytes), 2):
            if i + 1 < len(data_bytes):
                word = (data_bytes[i] << 8) | data_bytes[i + 1]
            else:
                word = data_bytes[i] << 8  # Odd byte at end
            words.append(word)

        return words

    def read_file(self, path: str) -> List[int]:
        """Read words from a file."""
        with open(path, 'r') as f:
            return self.read(f)
