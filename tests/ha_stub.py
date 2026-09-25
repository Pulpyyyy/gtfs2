"""Import one gtfs2 module without a Home Assistant install.

gtfs_helper imports homeassistant at module level, and importing it the plain
way (`from custom_components.gtfs2.gtfs_helper import ...`) also runs the
package `__init__.py`, which pulls in the coordinator, the platforms and with
them a good half of Home Assistant. Installing homeassistant answers all of
that, but it is a heavy pin for a suite whose subject is the integration's own
logic: `_interpret_departure_rows` reads a list of dicts and a few datetimes,
and never touches a hass object beyond `hass.config.time_zone`.

So the handful of homeassistant modules the import chain touches are registered
as stubs, and `load()` imports the wanted module on its own, under a synthetic
package whose `__path__` is the component directory, so its relative imports
(`from .const import ...`) still resolve while `__init__.py` never runs.

Only the functions the code actually calls carry behaviour: utcnow, now, as_utc,
utc_from_timestamp, get_time_zone and parse_datetime. Everything else is an
empty shell, and a shell that is reached raises instead of returning something
plausible: a harness that quietly answers for the code under test is worse than
no harness.

What this does not do is check that the integration uses Home Assistant
correctly. A stub cannot notice that an HA signature moved. It is meant for the
pure-logic cases; a test that exercises hass itself wants the real thing.

Nothing here is imported beyond the standard library, and if homeassistant does
turn out to be installed, `install()` steps aside and leaves the real modules in
place, so a test written against the stub keeps working in an environment that
has HA.

Using it
--------

pytest puts the tests directory on sys.path, so a test module next to this file
imports it by name and asks for the module it wants to exercise:

    import ha_stub

    gtfs_helper = ha_stub.load("gtfs_helper")
    result = gtfs_helper._interpret_departure_rows(hass, rows, ...)

install() runs on the first load, so a test that only wants dt_util calls it
itself before importing anything from homeassistant:

    ha_stub.install()
    import homeassistant.util.dt as dt_util

The clock the stub reads is `datetime.datetime.now`, which is what freezegun
replaces, so `freeze_time` drives dt_util.utcnow() and dt_util.now() as it does
with the real one. Where freezegun is not wanted, the stub can be pinned on its
own, in epoch seconds:

    ha_stub.freeze(datetime.datetime.fromisoformat(
        "2026-09-03T08:05:00+01:00").timestamp())

dt_util.now() answers in the default zone Home Assistant would have set at
startup, so a test that compares against local timetable strings sets it the
same way the integration does:

    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))

load() takes a component directory too. Pointing it at the copy in another
checkout, under its own alias, loads both in one process, which is how the
before and after of a change can be pushed the same fixture and diffed:

    old = ha_stub.load("gtfs_helper", component=other_checkout, alias="before")
    new = ha_stub.load("gtfs_helper", alias="after")

The whole of both suites can be run against another checkout's component
the same way: `pytest tests_provider/ --component path/to/custom_components/gtfs2`
sets COMPONENT, which every load() without a component reads.

Two errors say the same thing in different ways. An ImportError naming a
homeassistant module or symbol means install() does not carry it yet. An
AssertionError naming a stubbed symbol means the test reached one that is there
but empty. Either the test walked onto a path this rig was not built for, or
the stub has to grow a real answer. Returning something plausible to make the
run go through is how a harness starts lying about the code it measures.

Some of it should not grow. coordinator, sensor and config_flow are Home
Assistant plumbing, and what a test would check there is that the integration
speaks to HA correctly, which is exactly what a stub cannot answer for. Those
want the real thing, and install() steps aside as soon as it is installed.
"""
from __future__ import annotations

import datetime
import enum
import importlib.util
import re
import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "gtfs2"

_FROZEN: float | None = None
_DEFAULT_TIME_ZONE: datetime.tzinfo = datetime.timezone.utc
_DT_MODULE: types.ModuleType | None = None


def freeze(epoch_seconds: float | None) -> None:
    """Pin the stub clock, or hand it back to the wall clock with None."""
    global _FROZEN
    _FROZEN = None if epoch_seconds is None else float(epoch_seconds)


def frozen_at() -> float | None:
    return _FROZEN


def _utcnow() -> datetime.datetime:
    if _FROZEN is None:
        # datetime.now is what freezegun replaces, so freeze_time reaches here
        return datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(_FROZEN, datetime.timezone.utc)


def _utc_from_timestamp(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Match the real implementation: a naive datetime is assumed to be in
    the configured default zone, not the machine's own system timezone.
    `value.astimezone()` with no argument uses the latter, which silently
    produces a wrong offset whenever the two differ -- exactly the kind of
    error this whole test exists to catch, so the stub can't afford it.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_DEFAULT_TIME_ZONE)
    return value.astimezone(datetime.timezone.utc)


def _as_local(value: datetime.datetime) -> datetime.datetime:
    """An instant read in the configured zone; a naive one is taken as UTC,
    as the real implementation does."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(_DEFAULT_TIME_ZONE)


def _get_time_zone(name: str) -> datetime.tzinfo:
    """The tzinfo of a zone name, the way Home Assistant hands it out.

    Home Assistant answers None for a zone it cannot find, which here would
    quietly send every comparison back to UTC and shift a whole timetable by
    the agency's offset. A test would rather hear about it: a Windows checkout
    with no `tzdata` installed has no zone database at all, and the wrong
    answer would be a passing suite reading the wrong wall clock.
    """
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError as err:
        raise RuntimeError(
            f"no time zone {name!r}: either the name is wrong, or this "
            "machine has no IANA zone database (pip install tzdata)") from err


def _set_default_time_zone(time_zone: datetime.tzinfo) -> None:
    global _DEFAULT_TIME_ZONE
    _DEFAULT_TIME_ZONE = time_zone
    if _DT_MODULE is not None:
        _DT_MODULE.DEFAULT_TIME_ZONE = time_zone


def _now(time_zone: datetime.tzinfo | None = None) -> datetime.datetime:
    """The current instant read in a zone rather than in UTC.

    get_next_departure compares a timetable, which is written in the agency's
    local time, against this. Answering UTC here would move every departure by
    the offset of the network being read.
    """
    return _utcnow().astimezone(time_zone or _DEFAULT_TIME_ZONE)


def _parse_datetime(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _module(name: str, **attrs) -> types.ModuleType:
    """Register one stub module, and hang it off its parent by attribute.

    Both spellings of an import have to work: `import homeassistant.util.dt`
    reads sys.modules, while `from homeassistant.helpers import entity_registry`
    reads the attribute off the parent package.

    Every stub is a package, empty __path__ and all, so that an import of
    something below it is looked up rather than refused for the wrong reason,
    and a symbol the stub does not carry says what is missing instead of
    reading like a broken Home Assistant install.
    """
    module = types.ModuleType(name)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)

    def __getattr__(item: str):
        if item.startswith("__") and item.endswith("__"):
            # introspection, not the code under test asking for a symbol
            raise AttributeError(item)
        raise ImportError(
            f"{name}.{item} is not stubbed: add it to ha_stub.install(), or "
            "run this test against a real Home Assistant")

    module.__getattr__ = __getattr__
    sys.modules[name] = module
    parent_name, _, leaf = name.rpartition(".")
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], leaf, module)
    return module


class _Unreached:
    """Stands in for a symbol no test is expected to reach."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            f"the harness reached {self._what}, which is stubbed out: either "
            "the test drives a path this rig was not built for, or the stub "
            "has to grow")

    def __getattr__(self, item: str) -> "_Unreached":
        return _Unreached(f"{self._what}.{item}")


class _MissingStub:
    """Answers an unstubbed homeassistant import with a message that says so.

    Without this the import machinery reports "No module named
    'homeassistant.data_entry_flow'; 'homeassistant' is not a package", which
    reads like a broken install rather than a stub that has not been written.
    Registered on sys.meta_path only when the stubs are, and consulted only
    after sys.modules, so it never speaks for a module that is stubbed.
    """

    @staticmethod
    def find_spec(name: str, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(
                f"{name} is not stubbed: add what the test needs to "
                "ha_stub.install(), or run the test against a real Home "
                "Assistant. Modules like coordinator, sensor and config_flow "
                "are Home Assistant plumbing, and a stub cannot answer for "
                "them honestly")
        return None


class _DataUpdateCoordinator:
    """A real, minimal base for `DataUpdateCoordinator` -- not a behaviour
    stub. Stores exactly what `GTFSUpdateCoordinator.__init__` passes to
    `super().__init__(...)` and what `_async_update_data` reads
    (`self.hass`, `self.data`), with none of Home Assistant's actual
    update-scheduling loop. Calling `_async_update_data()` directly, as
    this harness does, never touches that loop anyway -- it's not
    needed, not faked as if it were.
    """

    def __init__(self, hass, logger, *, name, update_interval=None, **kwargs) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None


class _UpdateFailed(Exception):
    pass


def _invalid(message):
    import voluptuous as vol  # at call time: the stub itself stays stdlib
    return vol.Invalid(message)


# homeassistant.helpers.config_validation, the validators the service
# schemas use, as Home Assistant 2024.11 writes them: a schema test is
# about which calls pass, so these answer as the real ones do
def _cv_string(value):
    if value is None:
        raise _invalid("string value is None")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        raise _invalid("value should be a string")
    return str(value)


def _cv_boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.lower().strip()
        if value in ("1", "true", "yes", "on", "enable"):
            return True
        if value in ("0", "false", "no", "off", "disable"):
            return False
    elif isinstance(value, (int, float)):
        return value != 0
    raise _invalid(f"invalid boolean value {value}")


def _cv_time(value):
    """dt_util.parse_time: H:M or H:M:S, anything else refused."""
    if isinstance(value, datetime.time):
        return value
    if not isinstance(value, str):
        raise _invalid("Not a parseable type")
    parts = value.split(":")
    try:
        if len(parts) not in (2, 3):
            raise ValueError
        return datetime.time(*(int(part) for part in parts))
    except ValueError:
        raise _invalid(f"Invalid time specified: {value}") from None


_OBJECT_ID = r"(?!_)[\da-z_]+(?<!_)"
_VALID_ENTITY_ID = re.compile(r"^(?!.+__)" + _OBJECT_ID + r"\." + _OBJECT_ID + r"$")


def _cv_entity_id(value):
    str_value = _cv_string(value).lower()
    if _VALID_ENTITY_ID.match(str_value):
        return str_value
    raise _invalid(f"Entity ID {value} is an invalid entity ID")


class _EntityShell:
    """The base of the source's entities (update, button): no behaviour,
    so a test builds one and calls its own methods. What Home Assistant
    derives from them, state and attributes, is HA's to answer."""

    async def async_added_to_hass(self) -> None:
        """Entity's hook, empty in Home Assistant: a subclass extends it."""

    def async_on_remove(self, func) -> None:
        """As Entity keeps them: the functions to call on removal."""
        self.__dict__.setdefault("_on_remove", []).append(func)


class _UpdateEntityFeature(int):
    """The flag values of homeassistant.components.update."""
    INSTALL = 1
    SPECIFIC_VERSION = 2
    PROGRESS = 4
    BACKUP = 8


class _EntityCategory:
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class _Platform:
    BUTTON = "button"
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"
    UPDATE = "update"


# --- the sensor platform -----------------------------------------------------
# What sensor.py builds its entities on, so a test can build one and read
# what the integration's own code wrote into it: the native value, the
# attributes, the icon, the attribution. Home Assistant's side of it (the
# state string it derives, the recorder, the entity registry, the listener
# a coordinator entity registers) is not answered here, and writing the
# state raises unless the test itself says what a write is.

class _SensorDeviceClass:
    """The device classes sensor.py names, with Home Assistant's values;
    a class this does not carry raises AttributeError, as a typo would."""
    DATE = "date"
    TIMESTAMP = "timestamp"


class _SensorEntity:
    """The properties Home Assistant reads off a sensor entity, read the way
    Entity and SensorEntity read them: from the _attr_ fields, None when
    the entity never set one."""

    _attr_native_value = None
    _attr_attribution = None
    _attr_unique_id = None
    _attr_device_info = None
    _attr_entity_category = None

    @property
    def native_value(self):
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        return getattr(self, "_attr_extra_state_attributes", None)

    @property
    def device_class(self):
        return getattr(self, "_attr_device_class", None)

    @property
    def icon(self):
        return getattr(self, "_attr_icon", None)

    @property
    def attribution(self):
        return self._attr_attribution

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def entity_category(self):
        return self._attr_entity_category


class _CoordinatorEntity:
    """What CoordinatorEntity hands its subclass: the coordinator, and an
    update that ends in a state write. Registering the listener is Home
    Assistant's, so adding the entity registers nothing; the write is
    Home Assistant's too, and a test that drives an update sets
    async_write_ha_state on the entity to hear it."""

    async_write_ha_state = _Unreached("Entity.async_write_ha_state")

    def __init__(self, coordinator, context=None) -> None:
        self.coordinator = coordinator
        self.coordinator_context = context

    async def async_added_to_hass(self) -> None:
        return None

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


def _slugify(text, *, separator: str = "_") -> str:
    """homeassistant.util.slugify on the ASCII it is handed here (table
    column names): lower case, every run of other characters one separator.
    Home Assistant transliterates the rest with unidecode, which the
    standard library has not; accents are only stripped here."""
    import unicodedata
    if text == "" or text is None:
        return ""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", separator, ascii_text.lower()).strip(separator)
    return slug or "unknown"


def _install_sensor_platform() -> None:
    """Hang the sensor platform's symbols on the modules install() registered."""
    sys.modules["homeassistant.components.sensor"].SensorEntity = _SensorEntity
    sys.modules["homeassistant.components.sensor"].SensorDeviceClass = _SensorDeviceClass
    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity
    sys.modules["homeassistant.util"].slugify = _slugify

# --- end of the sensor platform ----------------------------------------------


# --- config flows ------------------------------------------------------------
# What a config flow and an options flow are built on, as Home Assistant
# 2026.2 builds them: the result each step hands back, key for key, the
# unique_id bookkeeping, the options flow's config_entry, and the selectors.
# A selector validates a submitted value the way the real one does: a pick
# among the options offered, a boolean, a number within its bounds, an
# entity of the domains allowed. What drives a flow (the manager that runs
# a submission through the screen's schema, calls the next step and files
# the entry) is the test's own; this only answers what a step calls.

class _FlowResultType(enum.StrEnum):
    FORM = "form"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"
    EXTERNAL_STEP = "external"
    EXTERNAL_STEP_DONE = "external_done"
    SHOW_PROGRESS = "progress"
    SHOW_PROGRESS_DONE = "progress_done"
    MENU = "menu"


class _AbortFlow(Exception):
    """Ends the flow on an abort, raised from anywhere inside a step."""

    def __init__(self, reason, description_placeholders=None) -> None:
        super().__init__(f"Flow aborted: {reason}")
        self.reason = reason
        self.description_placeholders = description_placeholders


class _FlowHandler:
    """The results a step returns, shaped as data_entry_flow.FlowHandler
    shapes them. The manager sets hass, handler, flow_id and context."""

    VERSION = 1
    MINOR_VERSION = 1
    hass = None
    handler = None
    flow_id = None
    cur_step = None
    init_step = "init"

    @property
    def source(self):
        return self.context.get("source")

    def _result(self, **fields):
        return {"flow_id": self.flow_id, "handler": self.handler, **fields}

    def async_show_form(self, *, step_id=None, data_schema=None, errors=None,
                        description_placeholders=None, last_step=None, preview=None):
        result = self._result(type=_FlowResultType.FORM, data_schema=data_schema,
                              errors=errors, description_placeholders=description_placeholders,
                              last_step=last_step, preview=preview)
        if step_id is not None:
            result["step_id"] = step_id
        return result

    def async_show_menu(self, *, step_id=None, menu_options, sort=False,
                        description_placeholders=None):
        import voluptuous as vol  # at call time: the stub itself stays stdlib
        result = self._result(type=_FlowResultType.MENU,
                              data_schema=vol.Schema({"next_step_id": vol.In(menu_options)}),
                              menu_options=menu_options, sort=sort,
                              description_placeholders=description_placeholders)
        if step_id is not None:
            result["step_id"] = step_id
        return result

    def async_show_progress(self, *, step_id=None, progress_action,
                            description_placeholders=None, progress_task=None):
        result = self._result(type=_FlowResultType.SHOW_PROGRESS,
                              progress_action=progress_action,
                              description_placeholders=description_placeholders,
                              progress_task=progress_task)
        if step_id is not None:
            result["step_id"] = step_id
        return result

    def async_show_progress_done(self, *, next_step_id):
        return self._result(type=_FlowResultType.SHOW_PROGRESS_DONE, step_id=next_step_id)

    def async_abort(self, *, reason, description_placeholders=None):
        return self._result(type=_FlowResultType.ABORT, reason=reason,
                            description_placeholders=description_placeholders)

    def async_create_entry(self, *, title=None, data, description=None,
                           description_placeholders=None):
        return self._result(type=_FlowResultType.CREATE_ENTRY, title=title, data=data,
                            description=description,
                            description_placeholders=description_placeholders,
                            version=self.VERSION, minor_version=self.MINOR_VERSION)

    def async_remove(self) -> None:
        """Called by the manager once the flow is over, however it ended."""


class _Handlers(dict):
    """config_entries.HANDLERS: the flow class of each domain."""

    def register(self, domain):
        def keep(cls):
            self[domain] = cls
            return cls
        return keep


_HANDLERS = _Handlers()


class _ConfigFlow(_FlowHandler):
    """config_entries.ConfigFlow: a unique_id refuses a second entry."""

    def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if domain is not None:
            _HANDLERS.register(domain)(cls)

    @property
    def unique_id(self):
        return self.context.get("unique_id")

    async def async_set_unique_id(self, unique_id=None, *, raise_on_progress=True):
        if unique_id is None:
            self.context["unique_id"] = None
            return None
        if raise_on_progress and any(
                flow["flow_id"] != self.flow_id and flow["context"].get("source") != "reauth"
                for flow in self.hass.config_entries.flow.async_progress_by_handler(
                    self.handler, include_uninitialized=True,
                    match_context={"unique_id": unique_id})):
            raise _AbortFlow("already_in_progress")
        self.context["unique_id"] = unique_id
        return self.hass.config_entries.async_entry_for_domain_unique_id(
            self.handler, unique_id)

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True, *,
                                       error="already_configured",
                                       description_placeholders=None) -> None:
        if self.unique_id is None:
            return
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            self.handler, self.unique_id)
        if entry is None:
            return
        if updates is not None:
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
        raise _AbortFlow(error, description_placeholders)

    def async_create_entry(self, *, title, data, description=None,
                           description_placeholders=None, options=None, subentries=None):
        result = super().async_create_entry(
            title=title, data=data, description=description,
            description_placeholders=description_placeholders)
        result["options"] = options or {}
        result["subentries"] = subentries or ()
        return result


class _OptionsFlow(_FlowHandler):
    """config_entries.OptionsFlow: the entry is looked up by the handler,
    which is its entry_id, once the manager has handed the flow a hass."""

    @property
    def config_entry(self):
        if self.hass is None:
            raise ValueError("The config entry is not available during initialisation")
        return self.hass.config_entries.async_get_known_entry(self.handler)


class _SelectSelectorMode(enum.StrEnum):
    LIST = "list"
    DROPDOWN = "dropdown"


class _NumberSelectorMode(enum.StrEnum):
    BOX = "box"
    SLIDER = "slider"


class _Selector:
    """A selector keeps its config, completed with the defaults the real
    CONFIG_SCHEMA fills in, and refuses a config the real one refuses."""

    def __init__(self, config=None) -> None:
        self.config = self._checked(dict(config or {}))

    def _checked(self, config):
        return config


class _SelectSelector(_Selector):
    """One of the options, or a list of them with multiple; any text as
    well with custom_value. The options are all strings, or all dicts of
    exactly a string value and a string label."""

    def _checked(self, config):
        options = config.get("options")
        if options is None:
            raise _invalid("required key not provided @ data['options']")
        as_text = all(isinstance(option, str) for option in options)
        as_dicts = all(isinstance(option, dict) and set(option) == {"value", "label"}
                       and all(isinstance(option[k], str) for k in option)
                       for option in options)
        if not (as_text or as_dicts):
            raise _invalid(f"options must all be strings or all value/label dicts: {options!r}")
        if "mode" in config:
            config["mode"] = _SelectSelectorMode(config["mode"]).value
        return {"multiple": False, "custom_value": False, "sort": False, **config}

    def __call__(self, data):
        import voluptuous as vol
        values = [option if isinstance(option, str) else option["value"]
                  for option in self.config["options"]]
        pick = vol.In(values)
        if self.config["custom_value"]:
            pick = vol.Any(pick, str)
        if not self.config["multiple"]:
            return pick(vol.Schema(str)(data))
        if not isinstance(data, list):
            raise vol.Invalid("Value should be a list")
        return [pick(vol.Schema(str)(value)) for value in data]


class _BooleanSelector(_Selector):
    def __call__(self, data):
        import voluptuous as vol
        return vol.Coerce(bool)(data)


class _NumberSelector(_Selector):
    """A float, within min and max when the config sets them."""

    def _checked(self, config):
        for bound in ("min", "max"):
            if bound in config:
                config[bound] = float(config[bound])
        config.setdefault("step", 1)
        if "mode" not in config:
            config["mode"] = "slider" if "min" in config and "max" in config else "box"
        config["mode"] = _NumberSelectorMode(config["mode"]).value
        if config["mode"] == "slider" and not ("min" in config and "max" in config):
            raise _invalid("min and max are required in slider mode")
        return config

    def __call__(self, data):
        import voluptuous as vol
        value = vol.Coerce(float)(data)
        if "min" in self.config and value < self.config["min"]:
            raise vol.Invalid(f"Value {value} is too small")
        if "max" in self.config and value > self.config["max"]:
            raise vol.Invalid(f"Value {value} is too large")
        return value


_ENTITY_UUID = re.compile(r"^[0-9a-f]{32}$")


class _EntitySelector(_Selector):
    """An entity id of an allowed domain, or a registry id, which is not
    looked up here; a list of them with multiple."""

    def _checked(self, config):
        return {"multiple": False, "reorder": False, **config}

    def __call__(self, data):
        def validate(value):
            text = _cv_string(value).lower()
            if _ENTITY_UUID.match(text):
                return text
            entity_id = _cv_entity_id(text)
            domains = self.config.get("domain") or []
            domains = [domains] if isinstance(domains, str) else list(domains)
            if domains and entity_id.split(".", 1)[0] not in domains:
                raise _invalid(f"Entity {entity_id} belongs to domain "
                               f"{entity_id.split('.', 1)[0]}, expected {domains}")
            for key, keep in (("include_entities", True), ("exclude_entities", False)):
                if key in self.config and (entity_id in self.config[key]) != keep:
                    raise _invalid(f"Entity {entity_id} is not allowed here")
            return entity_id

        if not self.config["multiple"]:
            return validate(data)
        if not isinstance(data, list):
            raise _invalid("Value should be a list")
        return [validate(value) for value in data]


class _TextSelector(_Selector):
    def _checked(self, config):
        return {"multiline": False, "multiple": False, **config}

    def __call__(self, data):
        import voluptuous as vol
        if not self.config["multiple"]:
            return vol.Schema(str)(data)
        if not isinstance(data, list):
            raise vol.Invalid("Value should be a list")
        return [vol.Schema(str)(value) for value in data]


def _install_config_flow() -> None:
    """Hang the flow bases on config_entries, and register the two modules
    only a flow reads: data_entry_flow and helpers.selector."""
    entries = sys.modules["homeassistant.config_entries"]
    entries.ConfigFlow = _ConfigFlow
    entries.OptionsFlow = _OptionsFlow
    entries.HANDLERS = _HANDLERS
    entries.SOURCE_USER = "user"
    _module("homeassistant.data_entry_flow", FlowResult=dict,
            FlowResultType=_FlowResultType, AbortFlow=_AbortFlow)
    # the config dicts are TypedDicts in Home Assistant: calling one makes a dict
    _module("homeassistant.helpers.selector",
            SelectSelector=_SelectSelector, SelectSelectorConfig=dict,
            SelectSelectorMode=_SelectSelectorMode, SelectOptionDict=dict,
            BooleanSelector=_BooleanSelector, BooleanSelectorConfig=dict,
            NumberSelector=_NumberSelector, NumberSelectorConfig=dict,
            NumberSelectorMode=_NumberSelectorMode,
            EntitySelector=_EntitySelector, EntitySelectorConfig=dict,
            TextSelector=_TextSelector, TextSelectorConfig=dict)

# --- end of the config flows -------------------------------------------------


def installed() -> bool:
    """Whether a Home Assistant, real or already stubbed, can be imported."""
    if "homeassistant" in sys.modules:
        return True
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        return False
    return True


def _gtfs_realtime_bindings_installed() -> bool:
    """Whether the real `gtfs-realtime-bindings` package (and, with it,
    `protobuf`) can be imported -- same "a real install wins" rule as
    `installed()` above, kept separate since this package has nothing
    to do with Home Assistant and can be present or absent independently.
    """
    if "google.transit.gtfs_realtime_pb2" in sys.modules:
        return True
    try:
        from google.transit import gtfs_realtime_pb2  # noqa: F401
    except ImportError:
        return False
    return True


def _pygtfs_installed() -> bool:
    """Whether the real `pygtfs` package (and, with it, `sqlalchemy`) can
    be imported. Same "a real install wins" rule as the checks above.
    """
    if "pygtfs" in sys.modules:
        return True
    try:
        import pygtfs  # noqa: F401
    except ImportError:
        return False
    return True


def install() -> None:
    """Register the homeassistant modules the gtfs2 import chain touches.

    A real Home Assistant wins: if one is importable, this does nothing at all,
    so the same test reads the real dt_util wherever it is available.
    """
    global _DT_MODULE
    if installed():
        return

    _module("homeassistant")
    _module(
        "homeassistant.const",
        CONF_OFFSET="offset",
        CONF_HOST="host",
        CONF_NAME="name",
        STATE_UNKNOWN="unknown",
        Platform=_Platform,
        ATTR_LATITUDE="latitude",
        ATTR_LONGITUDE="longitude",
        EntityCategory=_EntityCategory,
    )
    _module("homeassistant.util", Throttle=lambda *a, **k: (lambda fn: fn))
    _DT_MODULE = _module(
        "homeassistant.util.dt",
        utcnow=_utcnow,
        now=_now,
        as_utc=_as_utc,
        as_local=_as_local,
        utc_from_timestamp=_utc_from_timestamp,
        get_time_zone=_get_time_zone,
        set_default_time_zone=_set_default_time_zone,
        parse_datetime=_parse_datetime,
        DATE_STR_FORMAT="%Y-%m-%d",
        DEFAULT_TIME_ZONE=_DEFAULT_TIME_ZONE,
    )
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=_cv_string,
            boolean=_cv_boolean, time=_cv_time, entity_id=_cv_entity_id)
    _module("homeassistant.helpers.entity", Entity=object)
    _module("homeassistant.helpers.entity_registry",
            async_get=_Unreached("entity_registry.async_get"))
    _module("homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_DataUpdateCoordinator,
            UpdateFailed=_UpdateFailed)
    _module("homeassistant.helpers.translation",
            async_get_translations=_Unreached("async_get_translations"))
    _module("homeassistant.helpers.dispatcher",
            async_dispatcher_send=_Unreached("async_dispatcher_send"),
            async_dispatcher_connect=_Unreached("async_dispatcher_connect"))
    _module("homeassistant.helpers.device_registry",
            DeviceEntryType=types.SimpleNamespace(SERVICE="service"),
            DeviceInfo=dict,
            async_get=_Unreached("device_registry.async_get"))
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.restore_state", RestoreEntity=type("RestoreEntity", (), {}))
    _module("homeassistant.helpers.event",
            async_call_later=_Unreached("async_call_later"),
            async_track_time_change=_Unreached("async_track_time_change"))
    _module("homeassistant.components")
    _module("homeassistant.components.sensor",
            PLATFORM_SCHEMA=_Unreached("PLATFORM_SCHEMA"))
    _module("homeassistant.components.update",
            UpdateEntity=_EntityShell, UpdateEntityFeature=_UpdateEntityFeature)
    _module("homeassistant.components.button", ButtonEntity=_EntityShell)
    _module("homeassistant.components.switch", SwitchEntity=_EntityShell)
    _module("homeassistant.components.persistent_notification",
            async_create=_Unreached("persistent_notification.async_create"),
        async_dismiss=_Unreached("persistent_notification.async_dismiss"),
            create=_Unreached("persistent_notification.create"))
    _module("homeassistant.core", HomeAssistant=object, ServiceCall=object,
            SupportsResponse=_Unreached("SupportsResponse"),
            callback=lambda fn: fn)
    _module("homeassistant.config_entries", ConfigEntry=object,
            ConfigEntries=object, SOURCE_IMPORT="import")
    _module("homeassistant.exceptions", HomeAssistantError=Exception,
            PlatformNotReady=type("PlatformNotReady", (Exception,), {}))
    _install_sensor_platform()
    _install_config_flow()
    if _MissingStub not in sys.meta_path:
        sys.meta_path.append(_MissingStub)

    # gtfs_rt_helper.py imports this at module level (`from google.transit
    # import gtfs_realtime_pb2`), so gtfs_helper.py's own import of
    # gtfs_rt_helper.py pulls it in even for tests that never touch RT.
    # The only thing ever used from it, across the whole codebase, is
    # FeedMessage() -- and only inside the raw-protobuf fallback branch of
    # get_gtfs_feed_entities(), a function every current test replaces
    # entirely. Stubbed the same way as the Home Assistant symbols above:
    # a real install wins if present, and an unstubbed path raises instead
    # of returning something plausible.
    if not _gtfs_realtime_bindings_installed():
        _module("google")
        _module("google.transit")
        _module(
            "google.transit.gtfs_realtime_pb2",
            FeedMessage=_Unreached("gtfs_realtime_pb2.FeedMessage"),
        )
        # the feed reader imports its decode error at call time
        _module("google.protobuf")
        _module("google.protobuf.message",
                DecodeError=type("DecodeError", (Exception,), {}))

    # gtfs_helper.py imports pygtfs and sqlalchemy.sql.text at module level
    # (get_gtfs, _fetch_departure_rows), but every current test either
    # never calls those functions or replaces them with
    # unittest.mock.patch.object before they'd touch a real database --
    # same reasoning as the gtfs_realtime_pb2 stub above.
    if not _pygtfs_installed():
        _module("pygtfs",
                Schedule=_Unreached("pygtfs.Schedule"),
                append_feed=_Unreached("pygtfs.append_feed"))
        _module("sqlalchemy")
        _module("sqlalchemy.sql", text=_Unreached("sqlalchemy.sql.text"))


def load(module_name: str, component: str | Path | None = None,
         alias: str = "gtfs2_under_test") -> types.ModuleType:
    """Import one module of a gtfs2 component directory, on its own.

    The module is loaded as a submodule of a synthetic package whose __path__ is
    that directory, so its relative imports resolve there while the real
    __init__.py, which pulls in half of Home Assistant, never runs.

    alias names that package. Two directories loaded under two aliases stay
    apart in sys.modules, which is what lets the before and after of a change
    answer the same fixture in one process.
    """
    install()
    # read at call time: pytest --component points COMPONENT elsewhere
    component = Path(component if component is not None else COMPONENT)
    if alias not in sys.modules:
        package = types.ModuleType(alias)
        package.__path__ = [str(component)]
        sys.modules[alias] = package
    full = f"{alias}.{module_name}"
    if full in sys.modules:
        return sys.modules[full]
    path = component / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(full, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    if module_name == "__init__":
        # the package's own module reads __path__, as a package does
        module.__path__ = [str(component)]
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
