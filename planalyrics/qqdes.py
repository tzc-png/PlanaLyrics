"""QQ Music's QRC scramble, undone.

QRC is the one word-level lyric the sites here carry in a form that is neither
plain nor base64: a deflated document with the QQ "Triple-DES" laid over it. That
DES is a soft implementation with a key expansion of its own -- a standard
Triple-DES library does not undo it, and the difference is not a key or an
order, so it had to be brought over rather than called.

Ported from WXRIW/QQMusicDecoder's DESHelper.cs and Decrypter.cs (MIT). The bit
numbering, the two constants that make the expansion its own -- the ``- 27`` on
the second half of the compression table, and the mask the rotation is taken
with -- and the order the three passes run in are all as they are there, because
each of them is load-bearing: the same tables arranged the standard way leave the
payload as opaque as it began.

Two of the s-boxes are not the ones DES is usually written with -- ``_SBOX[1][23]``
is 15 where the standard table says 14, and ``_SBOX[3][53]`` is 10 where it says
1. They are left as they are found. The textbook values look like corrections and
are not: a hand copy of this file that "fixed" those two back to the standard
numbers decrypted a real payload into garbage.
"""

from __future__ import annotations

import zlib

ENCRYPT = 1
DECRYPT = 0

# the three 8-byte keys end to end, which is what the site's own decoder uses
KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)

_KEY_RND_SHIFT = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_KEY_PERM_C = (56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1,
               58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35)
_KEY_PERM_D = (62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5,
               60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3)
_KEY_COMPRESSION = (13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9,
                    22, 18, 11, 3, 25, 7, 15, 6, 26, 19, 12, 1,
                    40, 51, 30, 36, 46, 54, 29, 39, 50, 44, 32, 47,
                    43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31)

_IP_LEFT = (57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
            61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_IP_RIGHT = (56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2,
             60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6)

# where each bit of the f-function's output comes from, as (source bit, place)
_F_PERM = ((15, 0), (6, 1), (19, 2), (20, 3), (28, 4), (11, 5), (27, 6), (16, 7),
           (0, 8), (14, 9), (22, 10), (25, 11), (4, 12), (17, 13), (30, 14), (9, 15),
           (1, 16), (7, 17), (23, 18), (13, 19), (31, 20), (26, 21), (2, 22), (8, 23),
           (18, 24), (12, 25), (29, 26), (5, 27), (21, 28), (10, 29), (3, 30), (24, 31))


def _bitnum(data: bytes, bit: int, place: int) -> int:
    """One bit of an 8-byte block, counted from the top of each 32-bit half.

    The halves are read least significant byte first, so the block's own byte
    order is turned around twice over: bit 0 is the top bit of byte 3.
    """
    return ((data[bit // 32 * 4 + 3 - bit % 32 // 8] >> (7 - bit % 8)) & 1) << place


def _bitnum_intr(word: int, bit: int, place: int) -> int:
    return ((word >> (31 - bit)) & 1) << place


def _bitnum_intl(word: int, bit: int, place: int) -> int:
    return (((word << bit) & 0x80000000) >> place)


def _sboxbit(value: int) -> int:
    """The six-bit index an s-box is addressed with, out of order."""
    return (value & 0x20) | ((value & 0x1F) >> 1) | ((value & 0x01) << 4)


def _key_schedule(key: bytes, mode: int) -> list[list[int]]:
    schedule = [[0] * 6 for _ in range(16)]
    left = right = 0
    for index in range(28):
        left |= _bitnum(key, _KEY_PERM_C[index], 31 - index)
        right |= _bitnum(key, _KEY_PERM_D[index], 31 - index)

    for index in range(16):
        shift = _KEY_RND_SHIFT[index]
        left = ((left << shift) | (left >> (28 - shift))) & 0xFFFFFFF0
        right = ((right << shift) | (right >> (28 - shift))) & 0xFFFFFFF0
        target = 15 - index if mode == DECRYPT else index
        for position in range(24):
            schedule[target][position // 8] |= _bitnum_intr(
                left, _KEY_COMPRESSION[position], 7 - position % 8
            )
        for position in range(24, 48):
            schedule[target][position // 8] |= _bitnum_intr(
                right, _KEY_COMPRESSION[position] - 27, 7 - position % 8
            )
    return schedule


def _initial_permutation(block: bytes) -> tuple[int, int]:
    left = 0
    for place, bit in enumerate(_IP_LEFT):
        left |= _bitnum(block, bit, 31 - place)
    right = 0
    for place, bit in enumerate(_IP_RIGHT):
        right |= _bitnum(block, bit, 31 - place)
    return left, right


def _inverse_permutation(left: int, right: int) -> bytes:
    out = bytearray(8)
    for index in range(8):
        start = (index + 4) % 8
        byte = 0
        for step in range(4):
            bit = start + 8 * step
            byte |= _bitnum_intr(right, bit, 7 - 2 * step)
            byte |= _bitnum_intr(left, bit, 6 - 2 * step)
        out[index] = byte
    return bytes(out)


def _f(state: int, key: list[int]) -> int:
    expanded = (
        _bitnum_intl(state, 31, 0) | ((state & 0xF0000000) >> 1) | _bitnum_intl(state, 4, 5)
        | _bitnum_intl(state, 3, 6) | ((state & 0x0F000000) >> 3) | _bitnum_intl(state, 8, 11)
        | _bitnum_intl(state, 7, 12) | ((state & 0x00F00000) >> 5) | _bitnum_intl(state, 12, 17)
        | _bitnum_intl(state, 11, 18) | ((state & 0x000F0000) >> 7) | _bitnum_intl(state, 16, 23)
    )
    other = (
        _bitnum_intl(state, 15, 0) | ((state & 0x0000F000) << 15) | _bitnum_intl(state, 20, 5)
        | _bitnum_intl(state, 19, 6) | ((state & 0x00000F00) << 13) | _bitnum_intl(state, 24, 11)
        | _bitnum_intl(state, 23, 12) | ((state & 0x000000F0) << 11) | _bitnum_intl(state, 28, 17)
        | _bitnum_intl(state, 27, 18) | ((state & 0x0000000F) << 9) | _bitnum_intl(state, 0, 23)
    )
    mixed = [
        ((expanded >> 24) & 0xFF) ^ key[0],
        ((expanded >> 16) & 0xFF) ^ key[1],
        ((expanded >> 8) & 0xFF) ^ key[2],
        ((other >> 24) & 0xFF) ^ key[3],
        ((other >> 16) & 0xFF) ^ key[4],
        ((other >> 8) & 0xFF) ^ key[5],
    ]
    state = (
        (_SBOX[0][_sboxbit(mixed[0] >> 2)] << 28)
        | (_SBOX[1][_sboxbit(((mixed[0] & 0x03) << 4) | (mixed[1] >> 4))] << 24)
        | (_SBOX[2][_sboxbit(((mixed[1] & 0x0F) << 2) | (mixed[2] >> 6))] << 20)
        | (_SBOX[3][_sboxbit(mixed[2] & 0x3F)] << 16)
        | (_SBOX[4][_sboxbit(mixed[3] >> 2)] << 12)
        | (_SBOX[5][_sboxbit(((mixed[3] & 0x03) << 4) | (mixed[4] >> 4))] << 8)
        | (_SBOX[6][_sboxbit(((mixed[4] & 0x0F) << 2) | (mixed[5] >> 6))] << 4)
        | _SBOX[7][_sboxbit(mixed[5] & 0x3F)]
    )
    out = 0
    for bit, place in _F_PERM:
        out |= _bitnum_intl(state, bit, place)
    return out


def _crypt(block: bytes, key: list[list[int]]) -> bytes:
    left, right = _initial_permutation(block)
    for index in range(15):
        left, right = right, _f(right, key[index]) ^ left
    left = _f(right, key[15]) ^ left
    return _inverse_permutation(left, right)


def _triple_des_key_setup(key: bytes, mode: int) -> list[list[list[int]]]:
    """The three passes' subkeys, in the order they are applied.

    Decrypting runs the last key first and inside out -- the middle pass is the
    only one that encrypts -- which is what makes the three keys undo each other.
    """
    if mode == ENCRYPT:
        return [
            _key_schedule(key[0:8], ENCRYPT),
            _key_schedule(key[8:16], DECRYPT),
            _key_schedule(key[16:24], ENCRYPT),
        ]
    return [
        _key_schedule(key[16:24], DECRYPT),
        _key_schedule(key[8:16], ENCRYPT),
        _key_schedule(key[0:8], DECRYPT),
    ]


def decrypt(encoded: str) -> str:
    """Undo one QRC payload: hex to bytes, three passes, then inflate."""
    payload = bytes.fromhex(encoded)
    setup = _triple_des_key_setup(KEY, DECRYPT)
    plain = bytearray()
    for offset in range(0, len(payload), 8):
        block = payload[offset:offset + 8]
        for schedule in setup:
            block = _crypt(block, schedule)
        plain += block
    return zlib.decompress(bytes(plain)).decode("utf-8", "replace")
