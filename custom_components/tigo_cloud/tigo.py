"""Business Logic for Tigo."""

from datetime import datetime, timedelta, timezone
import logging

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import TIGO_URL

_LOGGER = logging.getLogger(__name__)

# Time between updating data
SCAN_INTERVAL = timedelta(minutes=1)


class CookieCache:
    """Manages the cookie / auth bearer and also returns the system."""

    def __init__(self, username: str, password: str, systemid: str) -> None:
        """Initialize the cache."""
        self._username = username
        self._password = password
        self._systemid = systemid
        self._system = None
        self._cookie = None
        self._cookieJar = aiohttp.CookieJar()
        self._validTill = datetime(1, 1, 1, tzinfo=timezone.utc)

    async def getAuthHeader(self) -> str:
        """Return the Auth header."""
        cookie = await self.__getCookie()
        return {"accept": "application/json", "Authorization": f"Bearer {cookie.value}"}

    def getSystem(self) -> any:
        """Return the system description."""
        return self._system

    def getCookieJar(self) -> any:
        """Return the cookieJar."""
        return self._cookieJar

    async def getSystemAsync(self) -> any:
        """Renutns sthe system description async, for config_flow."""
        if self._system is None:
            await self.__getCookie()

        return self._system

    def resetCookie(self) -> None:
        """Reset validity to get a new cookie."""
        self._validTill = datetime(1, 1, 1, tzinfo=timezone.utc)

    async def __getCookie(self) -> any:
        """Get the cookie from a web login."""
        now = datetime.now(timezone.utc)
        if self._validTill > now:
            return self._cookie

        async with aiohttp.ClientSession(cookie_jar=self._cookieJar) as session:
            request = await session.get(TIGO_URL)
            text = await request.text()
            for item in text.split("\n"):
                if "TIGO_CSRF_TOKEN" in item:
                    csfr = item.split('"')[1]
                    break

            formData = {
                "_csrf": csfr,
                "LoginFormModel[login]": self._username,
                "LoginFormModel[password]": self._password,
                "LoginFormModel[remember_me]": "0",
            }

            request = await session.post(TIGO_URL, data=formData)

            if request.status != 200:
                _LOGGER.error("Connection failed")

            for f in session.cookie_jar:
                if f.key == "wssJwt":
                    self._cookie = f
                    break

            request.close()

            if self._system is None:
                query = (
                    "/system/summary/config?system_id="
                    + self._systemid
                    + "&resourceId=config&v=0.1.0&_=0"
                )
                request = await session.get(TIGO_URL + query)
                self._system = await request.json()

        seconds = int(self._cookie["max-age"])
        self._validTill = now + timedelta(seconds=seconds, hours=-1)
        return self._cookie


class TigoData:
    def _init_energy_accumulators(self):
        self._energy_accumulators = {
            "solar_energy": 0.0,
            "home_energy": 0.0,
            "grid_import": 0.0,
            "grid_export": 0.0,
            "battery_charge": 0.0,
            "battery_discharge": 0.0,
        }
        self._last_update = None

    def _integrate_energy(self, key, power, now):
        if self._last_update is not None:
            elapsed = (now - self._last_update).total_seconds() / 3600.0 
            self._energy_accumulators[key] += (power * elapsed)

    """Manages the cookie / auth bearer and also returns the system."""

    def __init__(self, username: str, password: str, systemid: str) -> None:
        """Initialize the cache."""
        self._cookieCahe = CookieCache(username, password, systemid)
        self._systemId = systemid
        self._lastTime = None
        self._data = {}
        self._init_energy_accumulators()

    def get_value(self, graph) -> any:
        """Return the reading."""
        result = 0
        for serie in graph["series"]:
            if serie["id"] == "solar_total":
                for data in serie["data"]:
                    if data[1] is not None:
                        result = data[1]
                break
        return result

    async def fetch_data(self) -> None:
        """Get the data from the web api."""
        authHeader = await self._cookieCahe.getAuthHeader()
        cookieJar = self._cookieCahe.getCookieJar()
        async with aiohttp.ClientSession(cookie_jar=cookieJar) as session:
            date = datetime.today().date()
            query = f"/api/v4/system/summary/aggenergy?system_id={self._systemId}&date={date}"
            request = await session.get(TIGO_URL + query, headers=authHeader)
            if request.status != 200:
                self._cookieCahe.resetCookie()
                return

            val = await request.json()
            self._data["energyRaw"] = val
            self._data["energy"] = {"dataset": val["dataset"]}

            time = self._lastTime
            for x in val["datasetLastData"]:
                time = val["datasetLastData"][x][11:16]
                break
            if self._lastTime != time:
                self._lastTime = time
                for value in (
                    "pin",
                    "rssi",
                    "pwm",
                    "temp",
                    "vin",
                    "vout",
                    "iin",
                    "reclaimedPower",
                ):
                    try:
                        query = f"/api/v4/system/summary/lastvalue?system_id={self._systemId}&resourceId=lastValue-{date}-{value}-{time}&v=0.1.0&_=0"
                        request = await session.get(
                            TIGO_URL + query, headers=authHeader
                        )
                        self._data[value] = await request.json()
                    except Exception as e:
                        msg = f"{e.__class__} occurred details: {e}"
                        _LOGGER.warning(msg)
                        # nop

            # Fetch instant powers and integrate to energy (import/export, charge/discharge)
            try:
                sensor_query = f"/api/v4/data/aggregate?systemId={self._systemId}&aggregate=now&objectTypeIds[]=14&objectTypeIds[]=32&objectTypeIds[]=36&objectTypeIds[]=46&objectTypeIds[]=56&objectTypeIds[]=58&objectTypeIds[]=62&objectTypeIds[]=57"
                sensor_request = await session.get(TIGO_URL + sensor_query, headers=authHeader)
                if sensor_request.status == 200:
                    sensor_val = await sensor_request.json()
                    objectTypeIds = sensor_val.get("objectTypeIds", {})
                    now = datetime.now()
                    self._data["gridPower"] = objectTypeIds.get("14", [None])[0]
                    self._data["homePower"] = objectTypeIds.get("36", [None])[0]
                    self._data["batteryPercentage"] = objectTypeIds.get("46", [None])[0]
                    self._data["batteryPower"] = objectTypeIds.get("56", [None])[0]
                    self._data["solarPower"] = objectTypeIds.get("62", [None])[0]
                    self._data["objectTypeIds"] = objectTypeIds
                    self._data["dataAvailable"] = sensor_val.get("dataAvailable", False)
                    self._data["time"] = sensor_val.get("time", [None])[0]
                    if self._last_update is not None:
                        grid_power = self._data["gridPower"]
                        if grid_power is not None:
                            if grid_power > 0:
                                self._integrate_energy("grid_export", grid_power, now)
                            elif grid_power < 0:
                                self._integrate_energy("grid_import", abs(grid_power), now)
                        battery_power = self._data["batteryPower"]
                        if battery_power is not None:
                            if battery_power > 0:
                                self._integrate_energy("battery_charge", battery_power, now)
                            elif battery_power < 0:
                                self._integrate_energy("battery_discharge", abs(battery_power), now)
                        if self._data.get("solarPower") is not None:
                            self._integrate_energy("solar_energy", self._data["solarPower"], now)
                        if self._data.get("homePower") is not None:
                            self._integrate_energy("home_energy", self._data["homePower"], now)
                    self._last_update = now
                    self._data["solar_energy"] = self._energy_accumulators["solar_energy"]
                    self._data["home_energy"] = self._energy_accumulators["home_energy"]
                    self._data["grid_import"] = self._energy_accumulators["grid_import"]
                    self._data["grid_export"] = self._energy_accumulators["grid_export"]
                    self._data["battery_charge"] = self._energy_accumulators["battery_charge"]
                    self._data["battery_discharge"] = self._energy_accumulators["battery_discharge"]
                else:
                    _LOGGER.warning(f"Sensor aggregate fetch failed: {sensor_request.status}")
            except Exception as e:
                msg = f"Sensor aggregate fetch error: {e.__class__} details: {e}"
                _LOGGER.warning(msg)

            for agg in ("now", "hour", "day", "month", "year"):
                try:
                    query = f"/api/v4/data/aggregate?systemId={self._systemId}&view=gen&output=echart&type=bar&agg={agg}&start={date}&end={date}&reclaimed=true"
                    request = await session.get(TIGO_URL + query, headers=authHeader)
                    self._data[agg] = self.get_value(await request.json())
                except Exception as e:
                    msg = f"{e.__class__} occurred details: {e}"
                    _LOGGER.warning(msg)
                    # nop
            try:
                query = f"/fleet/system/overview/data-lifetime?sysid={self._systemId}&range=lifetime"
                request = await session.get(TIGO_URL + query, headers=authHeader)
                value = await request.json()
                self._data["allTime"] = value["energy"]
            except Exception as e:
                msg = f"{e.__class__} occurred details: {e}"
                _LOGGER.warning(msg)
                # nop

    def get_system(self) -> any:
        """Return the system data."""
        return self._cookieCahe.getSystem()

    def get_reading(self, property) -> any:
        """Return readings of property."""
        return self._data.get(property)["dataset"]

    def get_summary(self, property) -> any:
        """Retun summary reading."""
        return self._data.get(property)


# see https://developers.home-assistant.io/docs/integration_fetching_data/
class TigoCoordinator(DataUpdateCoordinator):
    """Represents Coordinator for this sensor."""

    def __init__(self, hass: HomeAssistant, tigo_data: TigoData) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="TigoCoordinator",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=SCAN_INTERVAL,
        )
        self.tigo_data = tigo_data

    async def _async_update_data(self):
        return await self.tigo_data.fetch_data()

    def get_panels(self) -> any:
        """Return the panels of the system."""
        system = self.tigo_data.get_system()
        return [x for x in system["system"]["objects"] if x.get("B") == 2]

    def get_reading(self, panelId, property) -> any:
        """Get the actual reaging."""
        reading = self.tigo_data.get_reading(property)
        return reading.get(str(panelId), None)

    def get_summary(self, property) -> any:
        """Get the summary reading."""
        return self.tigo_data.get_summary(property)

