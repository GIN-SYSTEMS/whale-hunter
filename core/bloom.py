"""
core/bloom.py
Mathematical Bloom Filter for checking massive address blacklists with ultra-low RAM usage.
"""
import hashlib
import math

class AddressBloomFilter:
    def __init__(self, capacity: int, error_rate: float = 0.001):
        # Calculate optimal size in bits
        self.size_bits = int(-(capacity * math.log(error_rate)) / (math.log(2) ** 2))
        self.hash_count = int((self.size_bits / capacity) * math.log(2))
        
        # Allocate bytearray
        self.size_bytes = (self.size_bits + 7) // 8
        self.buffer = bytearray(self.size_bytes)
    
    def _hashes(self, item: str) -> list[int]:
        encoded = item.encode('utf-8')
        h1 = int(hashlib.md5(encoded).hexdigest()[:16], 16)
        h2 = int(hashlib.sha256(encoded).hexdigest()[:16], 16)
        return [(h1 + i * h2) % self.size_bits for i in range(self.hash_count)]
        
    def add(self, item: str) -> None:
        """Add an address to the Bloom Filter."""
        for bit_idx in self._hashes(item):
            byte_idx = bit_idx // 8
            bit_offset = bit_idx % 8
            self.buffer[byte_idx] |= (1 << bit_offset)
            
    def __contains__(self, item: str) -> bool:
        """Check if an address is probably in the filter."""
        for bit_idx in self._hashes(item):
            byte_idx = bit_idx // 8
            bit_offset = bit_idx % 8
            if not (self.buffer[byte_idx] & (1 << bit_offset)):
                return False
        return True
