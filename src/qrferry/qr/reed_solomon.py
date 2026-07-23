"""GF(256) Reed-Solomon codec compatible with ZXing QR_CODE_FIELD_256."""
from __future__ import annotations

__all__ = ["ReedSolomonError", "encode", "decode"]


class ReedSolomonError(ValueError):
    """Reed-Solomon codeword cannot be corrected."""


class _Field:
    def __init__(self, primitive: int = 0x011D, size: int = 256, generator_base: int = 0):
        self.size = size
        self.generator_base = generator_base
        self.exp_table = [0] * (size * 2)
        self.log_table = [0] * size
        x = 1
        for i in range(size):
            self.exp_table[i] = x
            x <<= 1
            if x >= size:
                x = (x ^ primitive) & (size - 1)
        for i in range(size - 1):
            self.log_table[self.exp_table[i]] = i
        for i in range(size, size * 2):
            self.exp_table[i] = self.exp_table[i - (size - 1)]

    def exp(self, power: int) -> int:
        return self.exp_table[power % (self.size - 1)]

    def log(self, value: int) -> int:
        if value == 0:
            raise ReedSolomonError("log(0) 未定义")
        return self.log_table[value]

    def inverse(self, value: int) -> int:
        if value == 0:
            raise ReedSolomonError("0 没有乘法逆元")
        return self.exp_table[(self.size - 1) - self.log_table[value]]

    def multiply(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.exp_table[self.log_table[a] + self.log_table[b]]


_FIELD = _Field()


class _Poly:
    def __init__(self, coefficients: list[int]):
        first = 0
        while first < len(coefficients) - 1 and coefficients[first] == 0:
            first += 1
        self.coefficients = coefficients[first:]

    @property
    def degree(self) -> int:
        return len(self.coefficients) - 1

    @property
    def is_zero(self) -> bool:
        return self.coefficients[0] == 0

    def coefficient(self, degree: int) -> int:
        return self.coefficients[len(self.coefficients) - 1 - degree]

    def evaluate_at(self, value: int) -> int:
        if value == 0:
            return self.coefficient(0)
        if value == 1:
            result = 0
            for coefficient in self.coefficients:
                result ^= coefficient
            return result
        result = self.coefficients[0]
        for coefficient in self.coefficients[1:]:
            result = _FIELD.multiply(value, result) ^ coefficient
        return result

    def add(self, other: "_Poly") -> "_Poly":
        if self.is_zero:
            return other
        if other.is_zero:
            return self
        small, large = self.coefficients, other.coefficients
        if len(small) > len(large):
            small, large = large, small
        out = large.copy()
        offset = len(large) - len(small)
        for i, coefficient in enumerate(small):
            out[i + offset] ^= coefficient
        return _Poly(out)

    def multiply(self, other: "_Poly") -> "_Poly":
        if self.is_zero or other.is_zero:
            return _Poly([0])
        out = [0] * (len(self.coefficients) + len(other.coefficients) - 1)
        for i, a in enumerate(self.coefficients):
            for j, b in enumerate(other.coefficients):
                out[i + j] ^= _FIELD.multiply(a, b)
        return _Poly(out)

    def multiply_scalar(self, scalar: int) -> "_Poly":
        if scalar == 0:
            return _Poly([0])
        if scalar == 1:
            return self
        return _Poly([_FIELD.multiply(value, scalar) for value in self.coefficients])

    def multiply_monomial(self, degree: int, coefficient: int) -> "_Poly":
        if degree < 0:
            raise ValueError("degree 不能为负数")
        if coefficient == 0:
            return _Poly([0])
        return _Poly(
            [_FIELD.multiply(value, coefficient) for value in self.coefficients]
            + [0] * degree
        )


def _monomial(degree: int, coefficient: int) -> _Poly:
    if degree < 0:
        raise ValueError("degree 不能为负数")
    if coefficient == 0:
        return _Poly([0])
    return _Poly([coefficient] + [0] * degree)


def _generator(ecc_bytes: int) -> _Poly:
    generator = _Poly([1])
    for degree in range(ecc_bytes):
        generator = generator.multiply(_Poly([1, _FIELD.exp(degree)]))
    return generator


def encode(data: bytes, ecc_bytes: int) -> bytes:
    """Append ``ecc_bytes`` parity bytes to one at-most-255-byte codeword."""
    if not data or ecc_bytes <= 0 or len(data) + ecc_bytes > 255:
        raise ValueError("Reed-Solomon 参数无效")
    generator = _generator(ecc_bytes).coefficients
    work = list(data) + [0] * ecc_bytes
    for index in range(len(data)):
        coefficient = work[index]
        if coefficient == 0:
            continue
        for offset, factor in enumerate(generator):
            work[index + offset] ^= _FIELD.multiply(factor, coefficient)
    return data + bytes(work[-ecc_bytes:])


def decode(codeword: bytes, ecc_bytes: int) -> bytes:
    """Correct one codeword and return its data portion."""
    if ecc_bytes <= 0 or len(codeword) <= ecc_bytes or len(codeword) > 255:
        raise ValueError("Reed-Solomon 参数无效")
    received = list(codeword)
    poly = _Poly(received)
    syndrome_coefficients = [0] * ecc_bytes
    no_error = True
    for i in range(ecc_bytes):
        value = poly.evaluate_at(_FIELD.exp(i + _FIELD.generator_base))
        syndrome_coefficients[ecc_bytes - 1 - i] = value
        no_error = no_error and value == 0
    if no_error:
        return bytes(received[:-ecc_bytes])

    syndrome = _Poly(syndrome_coefficients)
    sigma, omega = _euclidean_algorithm(_monomial(ecc_bytes, 1), syndrome, ecc_bytes)
    locations = _error_locations(sigma)
    magnitudes = _error_magnitudes(omega, locations)
    for location, magnitude in zip(locations, magnitudes):
        position = len(received) - 1 - _FIELD.log(location)
        if position < 0:
            raise ReedSolomonError("错误位置超出码字")
        received[position] ^= magnitude

    corrected = _Poly(received)
    for i in range(ecc_bytes):
        if corrected.evaluate_at(_FIELD.exp(i + _FIELD.generator_base)) != 0:
            raise ReedSolomonError("纠错后校验仍失败")
    return bytes(received[:-ecc_bytes])


def _euclidean_algorithm(a: _Poly, b: _Poly, limit: int) -> tuple[_Poly, _Poly]:
    if a.degree < b.degree:
        a, b = b, a
    r_last, r = a, b
    t_last, t = _Poly([0]), _Poly([1])
    while r.degree >= limit // 2:
        r_last_last, t_last_last = r_last, t_last
        r_last, t_last = r, t
        if r_last.is_zero:
            raise ReedSolomonError("无法求解错误定位多项式")
        r = r_last_last
        quotient = _Poly([0])
        denominator_leading_term = r_last.coefficient(r_last.degree)
        inverse_denominator = _FIELD.inverse(denominator_leading_term)
        while r.degree >= r_last.degree and not r.is_zero:
            degree_diff = r.degree - r_last.degree
            scale = _FIELD.multiply(r.coefficient(r.degree), inverse_denominator)
            quotient = quotient.add(_monomial(degree_diff, scale))
            r = r.add(r_last.multiply_monomial(degree_diff, scale))
        t = quotient.multiply(t_last).add(t_last_last)

    sigma_at_zero = t.coefficient(0)
    if sigma_at_zero == 0:
        raise ReedSolomonError("错误定位多项式常数项为 0")
    inverse = _FIELD.inverse(sigma_at_zero)
    return t.multiply_scalar(inverse), r.multiply_scalar(inverse)


def _error_locations(error_locator: _Poly) -> list[int]:
    count = error_locator.degree
    if count == 1:
        return [error_locator.coefficient(1)]
    result: list[int] = []
    for value in range(1, _FIELD.size):
        if error_locator.evaluate_at(value) == 0:
            result.append(_FIELD.inverse(value))
            if len(result) == count:
                break
    if len(result) != count:
        raise ReedSolomonError("错误位置数量不匹配")
    return result


def _error_magnitudes(error_evaluator: _Poly, locations: list[int]) -> list[int]:
    result: list[int] = []
    for i, location in enumerate(locations):
        xi_inverse = _FIELD.inverse(location)
        denominator = 1
        for j, other in enumerate(locations):
            if i != j:
                denominator = _FIELD.multiply(denominator, 1 ^ _FIELD.multiply(other, xi_inverse))
        magnitude = _FIELD.multiply(
            error_evaluator.evaluate_at(xi_inverse),
            _FIELD.inverse(denominator),
        )
        if _FIELD.generator_base != 0:
            magnitude = _FIELD.multiply(magnitude, xi_inverse)
        result.append(magnitude)
    return result
