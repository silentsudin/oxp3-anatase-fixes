"""Minimal ACPI resource-template parser for the BF63112 SPI connection.

Why this exists
---------------
The Windows driver never hard-codes an SPI clock, mode, word size, chip
select or GPIO.  The SPB branch of ``BetterlifeFingerprintDevice.dll`` takes
them from the *translated* resources Windows built from the ACPI ``_CRS`` of
``\\_SB.PC00.SPI1.FPRT`` (see ``docs/hw/fingerprint-transport.md`` §4):

* a ``SpiSerialBus`` descriptor supplies connection speed, data bit length,
  clock phase, clock polarity, device selection and the resource-source
  (controller) name;
* the ``GpioInt`` descriptor is the sensor's interrupt line (the driver
  requires one and refuses to start without it);
* optionally a ``GpioIo`` descriptor is the reset line the driver drives
  through the Windows resource hub (``FUN_180016a10``).

On Linux the same values live in the raw ``_CRS`` byte template, which is
root-only on this host.  This module parses that byte buffer directly, so no
``iasl``/``acpica`` dependency is needed in the repository: extract the
resource buffer once (see ``docs/hw/fingerprint-transport.md``) and feed the
bytes here.

Layouts are the ACPI specification's, cross-checked against the Linux
implementation in ``drivers/acpi/acpica/amlresrc.h`` and
``drivers/acpi/acpica/rsserial.c`` (ACPI 6.x, large/small resource items).

Only descriptors that matter for this device are decoded.  Everything else is
reported as an opaque item, so a template with extra resources still parses.
"""

from __future__ import annotations

import dataclasses
import enum

# ---------------------------------------------------------------------------
# Resource item names (ACPI 6.x, "Resource Type" byte values)
# ---------------------------------------------------------------------------

#: Small item names (the high five bits of the first byte).
SMALL_IRQ = 0x04
SMALL_DMA = 0x05
SMALL_START_DEPENDENT = 0x06
SMALL_END_DEPENDENT = 0x07
SMALL_IO = 0x08
SMALL_FIXED_IO = 0x09
SMALL_FIXED_DMA = 0x0A
SMALL_VENDOR = 0x0E
SMALL_END_TAG = 0x0F

#: Large item names (the low seven bits of the first byte).
LARGE_MEMORY24 = 0x01
LARGE_GENERIC_REGISTER = 0x02
LARGE_VENDOR = 0x03
LARGE_MEMORY32 = 0x04
LARGE_FIXED_MEMORY32 = 0x05
LARGE_ADDRESS32 = 0x06
LARGE_ADDRESS16 = 0x07
LARGE_EXTENDED_IRQ = 0x08
LARGE_ADDRESS64 = 0x09
LARGE_EXTENDED_ADDRESS64 = 0x0A
LARGE_EXTENDED_SPACE = 0x0B
LARGE_GPIO = 0x0C
LARGE_PIN_FUNCTION = 0x0D
LARGE_SERIAL_BUS = 0x0E
LARGE_PIN_CONFIG = 0x0F
LARGE_PIN_GROUP = 0x10
LARGE_PIN_GROUP_FUNCTION = 0x11
LARGE_PIN_GROUP_CONFIG = 0x12
LARGE_CLOCK_INPUT = 0x13

#: Serial bus types inside a SerialBus descriptor.
SERIAL_TYPE_I2C = 1
SERIAL_TYPE_SPI = 2
SERIAL_TYPE_UART = 3
SERIAL_TYPE_CSI2 = 4

SERIAL_TYPE_NAMES = {
    SERIAL_TYPE_I2C: "I2C",
    SERIAL_TYPE_SPI: "SPI",
    SERIAL_TYPE_UART: "UART",
    SERIAL_TYPE_CSI2: "CSI2",
}

#: SPI type-specific flags (u16 at SerialBus data offset 4).
SPI_FLAG_WIRE_MODE_3 = 1 << 0          # 0 = four-wire, 1 = three-wire
SPI_FLAG_DEVICE_POLARITY_HIGH = 1 << 1  # 0 = active low, 1 = active high

#: GPIO connection types.
GPIO_TYPE_INT = 0
GPIO_TYPE_IO = 1

#: GPIO interrupt-flags bit fields (``int_flags``).
GPIO_TRIGGERING_MASK = 0x1             # 0 = level, 1 = edge
GPIO_POLARITY_SHIFT = 1
GPIO_POLARITY_MASK = 0x3               # 0 = active high, 1 = active low, 2 = both
GPIO_SHAREABLE = 1 << 3
GPIO_WAKE_CAPABLE = 1 << 4
GPIO_IO_RESTRICTION_MASK = 0x3         # for GpioIo only


class AcpiError(ValueError):
    """A resource template is malformed or truncated."""


class TruncatedResource(AcpiError):
    """A descriptor claims more bytes than the template contains."""


class Triggering(enum.Enum):
    LEVEL = "level"
    EDGE = "edge"


class Polarity(enum.Enum):
    ACTIVE_HIGH = "active-high"
    ACTIVE_LOW = "active-low"
    ACTIVE_BOTH = "active-both"


class IoRestriction(enum.Enum):
    NONE = "none"
    INPUT = "input-only"
    OUTPUT = "output-only"
    NONE_PRESERVE = "none-preserve"


@dataclasses.dataclass(frozen=True)
class Resource:
    """One decoded resource item."""

    kind: str                    # "spi", "gpio-int", "gpio-io", "irq", ...
    offset: int                  # byte offset of the item in the template
    length: int                  # total item bytes, including the header
    # Serial bus -----------------------------------------------------------
    serial_type: int | None = None
    revision: int | None = None
    resource_source_index: int | None = None
    resource_source: str | None = None
    general_flags: int | None = None
    type_specific_flags: int | None = None
    type_revision: int | None = None
    type_data_length: int | None = None
    connection_speed_hz: int | None = None
    data_bit_length: int | None = None
    clock_phase: int | None = None
    clock_polarity: int | None = None
    device_selection: int | None = None
    # GPIO -----------------------------------------------------------------
    connection_type: int | None = None
    pins: tuple[int, ...] = ()
    triggering: Triggering | None = None
    polarity: Polarity | None = None
    shareable: bool | None = None
    wake_capable: bool | None = None
    io_restriction: IoRestriction | None = None
    pin_config: int | None = None
    debounce_ms: int | None = None
    drive_strength: int | None = None
    # IRQ ------------------------------------------------------------------
    irqs: tuple[int, ...] = ()
    irq_flags: int | None = None

    # -- derived SPI values ------------------------------------------------
    @property
    def spi_mode(self) -> int | None:
        """Linux ``SPI_MODE_*`` value: ``(CPOL << 1) | CPHA``.

        ACPI uses 0 = first phase / start low and 1 = second phase / start
        high, which maps 1:1 onto Linux CPHA/CPOL.
        """
        if self.clock_phase is None or self.clock_polarity is None:
            return None
        return ((self.clock_polarity & 1) << 1) | (self.clock_phase & 1)

    @property
    def spi_mode_name(self) -> str | None:
        mode = self.spi_mode
        return None if mode is None else f"SPI mode {mode}"

    @property
    def is_spi(self) -> bool:
        return self.kind == "serial-bus" and self.serial_type == SERIAL_TYPE_SPI


@dataclasses.dataclass(frozen=True)
class ResourceTemplate:
    """The decoded result of one ``_CRS`` byte buffer."""

    resources: tuple[Resource, ...]
    trailing: bytes = b""

    def of_kind(self, kind: str) -> tuple[Resource, ...]:
        return tuple(r for r in self.resources if r.kind == kind)

    @property
    def spi(self) -> Resource | None:
        """The first ``SpiSerialBus`` descriptor, if any."""
        for r in self.resources:
            if r.is_spi:
                return r
        return None

    @property
    def gpio_interrupts(self) -> tuple[Resource, ...]:
        return tuple(r for r in self.resources
                     if r.kind == "gpio" and r.connection_type == GPIO_TYPE_INT)

    @property
    def gpio_io(self) -> tuple[Resource, ...]:
        return tuple(r for r in self.resources
                     if r.kind == "gpio" and r.connection_type == GPIO_TYPE_IO)

    def describe(self) -> list[str]:
        """Human-readable lines, used by ``oxp3-fp-probe acpi_info``."""
        lines: list[str] = []
        for r in self.resources:
            if r.kind == "serial-bus":
                bus = SERIAL_TYPE_NAMES.get(r.serial_type, f"type {r.serial_type}")
                lines.append(f"SerialBus {bus} @{r.offset:#x} len {r.length}")
                if r.resource_source:
                    lines.append(f"  resource source : {r.resource_source}")
                lines.append(f"  revision        : {r.revision}")
                lines.append(f"  general flags   : 0x{r.general_flags:02x}"
                             if r.general_flags is not None else "  general flags: ?")
                if r.is_spi:
                    lines.append(f"  connection speed: {r.connection_speed_hz} Hz")
                    lines.append(f"  data bit length : {r.data_bit_length} bits")
                    lines.append(f"  clock phase     : {r.clock_phase}")
                    lines.append(f"  clock polarity  : {r.clock_polarity}")
                    lines.append(f"  device selection: {r.device_selection}")
                    lines.append(f"  => Linux mode   : {r.spi_mode} "
                                 f"({r.spi_mode_name})")
                    lines.append(f"  type flags      : 0x{r.type_specific_flags:04x}"
                                 if r.type_specific_flags is not None
                                 else "  type flags: ?")
            elif r.kind == "gpio":
                what = "GpioInt" if r.connection_type == GPIO_TYPE_INT else "GpioIo"
                pins = ", ".join(str(p) for p in r.pins) or "?"
                lines.append(f"{what} @{r.offset:#x} len {r.length} pins [{pins}]")
                if r.resource_source:
                    lines.append(f"  resource source : {r.resource_source}")
                if r.connection_type == GPIO_TYPE_INT:
                    lines.append(f"  triggering      : {r.triggering.value}"
                                 if r.triggering else "  triggering      : ?")
                    lines.append(f"  polarity        : {r.polarity.value}"
                                 if r.polarity else "  polarity        : ?")
                    lines.append(f"  shareable       : {r.shareable}")
                    lines.append(f"  wake capable    : {r.wake_capable}")
                else:
                    lines.append(f"  io restriction  : {r.io_restriction.value}"
                                 if r.io_restriction else "  io restriction: ?")
                    lines.append(f"  pin config      : {r.pin_config}")
            elif r.kind == "irq":
                lines.append(f"IRQ @{r.offset:#x} irqs {list(r.irqs)} "
                             f"flags 0x{(r.irq_flags or 0):02x}")
            elif r.kind == "extended-irq":
                lines.append(f"ExtendedIRQ @{r.offset:#x} irqs {list(r.irqs)}")
            else:
                lines.append(f"{r.kind} @{r.offset:#x} len {r.length} (skipped)")
        return lines


# ---------------------------------------------------------------------------
# Byte helpers
# ---------------------------------------------------------------------------

def _u8(buf: bytes | bytearray | memoryview, off: int) -> int:
    try:
        return buf[off]
    except IndexError:
        raise TruncatedResource(f"need 1 byte at {off:#x}") from None


def _u16(buf: bytes | bytearray | memoryview, off: int) -> int:
    if off + 2 > len(buf):
        raise TruncatedResource(f"need 2 bytes at {off:#x}")
    return buf[off] | (buf[off + 1] << 8)


def _u32(buf: bytes | bytearray | memoryview, off: int) -> int:
    if off + 4 > len(buf):
        raise TruncatedResource(f"need 4 bytes at {off:#x}")
    return (buf[off] | (buf[off + 1] << 8)
            | (buf[off + 2] << 16) | (buf[off + 3] << 24))


def _cstring(buf: bytes | bytearray | memoryview, off: int) -> str:
    """NUL-terminated ASCII string at ``off`` (empty when out of range)."""
    if off <= 0 or off >= len(buf):
        return ""
    end = off
    while end < len(buf) and buf[end] != 0:
        end += 1
    raw = bytes(buf[off:end])
    return raw.decode("ascii", "replace")


# ---------------------------------------------------------------------------
# Descriptor decoders
# ---------------------------------------------------------------------------

def _decode_serial_bus(off: int, data: memoryview, total: int) -> Resource:
    """SerialBus (0x8E): common 9-byte header + per-bus type data.

    Absolute descriptor layout (ACPI 6.x, AML as consumed by ACPICA):

        0   0x8E
        1   length (u16, bytes following the 3-byte header)
        3   revision id
        4   resource source index
        5   serial bus type (1 I2C, 2 SPI, 3 UART, 4 CSI2)
        6   general flags (bit0 slave, bit1 producer/consumer, bit2 sharing)
        7   type-specific flags (u16)
        9   type revision id
        10  type data length (u16)
        12  type-specific data (SPI: speed u32, bitlen u8, phase u8, pol u8,
            device selection u16; 9 bytes)
        21  vendor data (type_data_length - 9 bytes, may be empty)
        12 + type_data_length  resource source string (NUL terminated)

    The string offset is ACPICA's ``MOVE_SERIAL_RES`` rule:
    ``aml + type_data_length + sizeof(aml_resource_common_serialbus)``, i.e.
    12 + type_data_length here (``data`` starts at absolute 3).
    """
    if len(data) < 9:
        raise TruncatedResource(f"SerialBus at {off:#x} has only {len(data)} B")
    revision = data[0]
    res_index = data[1]
    serial_type = data[2]
    general = data[3]
    type_flags = _u16(data, 4)
    type_revision = data[6]
    type_data_length = _u16(data, 7)

    resource = Resource(
        kind="serial-bus", offset=off, length=total,
        serial_type=serial_type, revision=revision,
        resource_source_index=res_index, general_flags=general,
        type_revision=type_revision, type_data_length=type_data_length,
        type_specific_flags=type_flags,
    )

    source = ""
    if res_index:
        # data starts at absolute 3; string starts at absolute 12 + td.
        source_rel = 9 + type_data_length
        if source_rel < len(data):
            source = _cstring(data, source_rel)

    if serial_type == SERIAL_TYPE_SPI:
        if len(data) < 18:
            raise TruncatedResource(f"SPI SerialBus at {off:#x} too short")
        return dataclasses.replace(
            resource,
            connection_speed_hz=_u32(data, 9),
            data_bit_length=data[13],
            clock_phase=data[14],
            clock_polarity=data[15],
            device_selection=_u16(data, 16),
            resource_source=source,
        )
    if serial_type == SERIAL_TYPE_I2C:
        if len(data) < 15:
            raise TruncatedResource(f"I2C SerialBus at {off:#x} too short")
        return dataclasses.replace(
            resource,
            connection_speed_hz=_u32(data, 9),
            device_selection=_u16(data, 13),  # slave address, same slot
            resource_source=source,
        )
    return dataclasses.replace(resource, resource_source=source)


def _decode_gpio(off: int, data: memoryview, total: int) -> Resource:
    """GPIO connection (0x8C): GpioInt (type 0) or GpioIo (type 1).

    Absolute layout: 3-byte large header, then data:

        0   revision id
        1   connection type (0 int, 1 io)
        2   general flags (bit0 producer/consumer)
        4   interrupt/io flags (u16)
        6   pin config
        7   drive strength (u16)
        9   debounce timeout (u16)
        11  pin table offset (u16, absolute in the descriptor)
        13  resource source index
        14  resource source offset (u16, absolute)
        16  vendor offset (u16, absolute)
        18  vendor length (u16)
    """
    if len(data) < 20:
        raise TruncatedResource(f"GPIO at {off:#x} has only {len(data)} B")
    revision = data[0]
    connection_type = data[1]
    int_flags = _u16(data, 4)
    pin_table_off = _u16(data, 11)
    res_index = data[13]
    res_off = _u16(data, 14)
    vendor_off = _u16(data, 16)
    vendor_len = _u16(data, 18)

    # Offsets in the descriptor are absolute (they include the 3-byte header);
    # ``data`` starts at absolute 3.
    def absolute(value: int) -> int:
        return value - 3

    pins: tuple[int, ...] = ()
    if pin_table_off:
        start = absolute(pin_table_off)
        end_abs = 0
        for candidate in (res_off, vendor_off):
            if candidate:
                end_abs = candidate
                break
        if not end_abs:
            end_abs = total
        end = absolute(end_abs)
        if start < 0 or end > len(data) or end < start:
            raise TruncatedResource(f"GPIO pin table at {off:#x} out of range")
        pins = tuple(_u16(data, i) for i in range(start, end - 1, 2))

    source = _cstring(data, absolute(res_off)) if res_index else ""

    fields = dict(
        connection_type=connection_type, pins=pins, pin_config=data[6],
        drive_strength=_u16(data, 7), debounce_ms=_u16(data, 9),
        resource_source=source, revision=revision,
    )
    if connection_type == GPIO_TYPE_INT:
        fields.update(
            triggering=Triggering.EDGE if int_flags & GPIO_TRIGGERING_MASK
            else Triggering.LEVEL,
            polarity=Polarity({
                0: Polarity.ACTIVE_HIGH,
                1: Polarity.ACTIVE_LOW,
                2: Polarity.ACTIVE_BOTH,
            }.get((int_flags >> GPIO_POLARITY_SHIFT) & GPIO_POLARITY_MASK,
                  Polarity.ACTIVE_HIGH)),
            shareable=bool(int_flags & GPIO_SHAREABLE),
            wake_capable=bool(int_flags & GPIO_WAKE_CAPABLE),
        )
    else:
        fields.update(
            io_restriction={
                0: IoRestriction.NONE,
                1: IoRestriction.INPUT,
                2: IoRestriction.OUTPUT,
                3: IoRestriction.NONE_PRESERVE,
            }.get(int_flags & GPIO_IO_RESTRICTION_MASK, IoRestriction.NONE),
        )
    return Resource(kind="gpio", offset=off, length=total, **fields)


def _decode_extended_irq(off: int, data: memoryview, total: int) -> Resource:
    """Extended IRQ (0x89): flags, count, then u32 IRQ numbers."""
    if len(data) < 2:
        raise TruncatedResource(f"ExtendedIRQ at {off:#x} too short")
    flags = data[0]
    count = data[1]
    if len(data) < 2 + 4 * count:
        raise TruncatedResource(f"ExtendedIRQ at {off:#x} truncated")
    irqs = tuple(_u32(data, 2 + 4 * i) for i in range(count))
    return Resource(kind="extended-irq", offset=off, length=total,
                    irq_flags=flags, irqs=irqs)


def _decode_small_irq(off: int, data: memoryview, total: int) -> Resource:
    """Small IRQ (0x22/0x23): 16-bit mask, optional flags byte."""
    if len(data) < 2:
        raise TruncatedResource(f"IRQ at {off:#x} too short")
    mask = _u16(data, 0)
    irqs = tuple(i for i in range(16) if mask & (1 << i))
    return Resource(kind="irq", offset=off, length=total, irqs=irqs,
                    irq_flags=data[2] if len(data) > 2 else None)


# ---------------------------------------------------------------------------
# Template walker
# ---------------------------------------------------------------------------

def parse_resource_template(buf: bytes | bytearray | memoryview) -> ResourceTemplate:
    """Decode a raw ``_CRS`` resource template (the AML ``Return`` payload).

    ``buf`` is the byte buffer inside the ``_CRS`` ``Return (ResourceTemplate
    () {...})``: exactly the descriptor stream, without the ``Return`` opcode.
    """
    raw = bytes(buf)
    view = memoryview(raw)
    resources: list[Resource] = []
    off = 0
    while off < len(view):
        tag = view[off]
        if tag == 0x79:  # EndTag (small item 0x0F, length 1: checksum)
            if off + 2 > len(view):
                raise TruncatedResource(f"EndTag at {off:#x} truncated")
            off += 2
            if off < len(view):
                resources.append(Resource(kind="trailing-bytes", offset=off,
                                          length=len(view) - off))
                off = len(view)
            break
        if tag & 0x80:  # large item
            name = tag & 0x7F
            if off + 3 > len(view):
                raise TruncatedResource(f"large header at {off:#x} truncated")
            length = _u16(view, off + 1)
            total = 3 + length
            if off + total > len(view):
                raise TruncatedResource(
                    f"large item 0x{tag:02x} at {off:#x} claims {length} B, "
                    f"only {len(view) - off - 3} left")
            data = view[off + 3:off + total]
            if name == LARGE_SERIAL_BUS:
                resources.append(_decode_serial_bus(off, data, total))
            elif name == LARGE_GPIO:
                resources.append(_decode_gpio(off, data, total))
            elif name == LARGE_EXTENDED_IRQ:
                resources.append(_decode_extended_irq(off, data, total))
            else:
                resources.append(Resource(kind=_LARGE_NAMES.get(
                    name, f"large-0x{name:02x}"), offset=off, length=total))
            off += total
            continue
        # small item: name in the high five bits, length in the low three
        name = tag >> 3
        length = tag & 0x07
        total = 1 + length
        if off + total > len(view):
            raise TruncatedResource(
                f"small item 0x{tag:02x} at {off:#x} truncated")
        data = view[off + 1:off + total]
        if name == SMALL_IRQ:
            resources.append(_decode_small_irq(off, data, total))
        else:
            resources.append(Resource(kind=_SMALL_NAMES.get(
                name, f"small-0x{name:02x}"), offset=off, length=total))
        off += total
    else:
        # No EndTag: not fatal (some firmware omits it), but worth reporting.
        pass
    return ResourceTemplate(resources=tuple(resources))


_LARGE_NAMES = {
    LARGE_MEMORY24: "memory24",
    LARGE_GENERIC_REGISTER: "generic-register",
    LARGE_VENDOR: "vendor",
    LARGE_MEMORY32: "memory32",
    LARGE_FIXED_MEMORY32: "fixed-memory32",
    LARGE_ADDRESS32: "address32",
    LARGE_ADDRESS16: "address16",
    LARGE_ADDRESS64: "address64",
    LARGE_EXTENDED_ADDRESS64: "extended-address64",
    LARGE_EXTENDED_SPACE: "extended-space",
    LARGE_PIN_FUNCTION: "pin-function",
    LARGE_PIN_CONFIG: "pin-config",
    LARGE_PIN_GROUP: "pin-group",
    LARGE_PIN_GROUP_FUNCTION: "pin-group-function",
    LARGE_PIN_GROUP_CONFIG: "pin-group-config",
    LARGE_CLOCK_INPUT: "clock-input",
}

_SMALL_NAMES = {
    SMALL_DMA: "dma",
    SMALL_START_DEPENDENT: "start-dependent",
    SMALL_END_DEPENDENT: "end-dependent",
    SMALL_IO: "io",
    SMALL_FIXED_IO: "fixed-io",
    SMALL_FIXED_DMA: "fixed-dma",
    SMALL_VENDOR: "vendor-small",
}

#: A convenient summary of what the transport needs from a template.
@dataclasses.dataclass(frozen=True)
class SpiConnection:
    """Everything an SPI driver needs, extracted from one ``_CRS``."""

    controller: str | None
    speed_hz: int | None
    mode: int | None
    bits_per_word: int | None
    chip_select: int | None
    irq_pin: int | None
    irq_triggering: Triggering | None
    irq_polarity: Polarity | None
    irq_shareable: bool | None
    reset_pin: int | None
    reset_active_low: bool | None

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def spi_connection(template: ResourceTemplate) -> SpiConnection:
    """Reduce a parsed template to the SPI connection parameters.

    The reset line is only reported when a ``GpioIo`` descriptor exists; a
    board may instead expose it as ``reset-gpios`` or not at all.  The Windows
    driver obtains it through the resource hub, which corresponds to a
    ``GpioIo`` on the ACPI side.
    """
    spi = template.spi
    ints = template.gpio_interrupts
    ios = template.gpio_io
    gpio_int = ints[0] if ints else None
    gpio_io = ios[0] if ios else None
    return SpiConnection(
        controller=spi.resource_source if spi else None,
        speed_hz=spi.connection_speed_hz if spi else None,
        mode=spi.spi_mode if spi else None,
        bits_per_word=spi.data_bit_length if spi else None,
        chip_select=spi.device_selection if spi else None,
        irq_pin=(gpio_int.pins[0] if gpio_int and gpio_int.pins else None),
        irq_triggering=gpio_int.triggering if gpio_int else None,
        irq_polarity=gpio_int.polarity if gpio_int else None,
        irq_shareable=gpio_int.shareable if gpio_int else None,
        reset_pin=(gpio_io.pins[0] if gpio_io and gpio_io.pins else None),
        # GpioIo has no polarity field; the conventional ACPI reset is active
        # low.  Report None so callers do not guess silently.
        reset_active_low=None,
    )
