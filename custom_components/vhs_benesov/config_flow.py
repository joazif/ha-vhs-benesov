"""Config flow integrace VHS Benešov."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import InvalidAuth, VhsBenesovClient, VhsError
from .const import (
    COMPARE_MONTHS_CHOICES,
    CONF_COMPARE_MONTHS,
    CONF_IMPORT_HISTORY,
    CONF_UNITS,
    DEFAULT_COMPARE_MONTHS,
    DEFAULT_UNITS,
    DOMAIN,
    UNITS_CHOICES,
)

_LOGGER = logging.getLogger(__name__)

def _selector(values: tuple, translation_key: str) -> SelectSelector:
    """Rozbalovací výběr; popisky jsou v překladech (sekce selector)."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[str(v) for v in values],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key=translation_key,
        )
    )


def _compare_selector() -> SelectSelector:
    """Výběr, za kolik měsíců se srovnává spotřeba s loňskem."""
    return _selector(COMPARE_MONTHS_CHOICES, "compare_months")


def _units_selector() -> SelectSelector:
    """Jednotky zobrazení: litry nebo m³."""
    return _selector(UNITS_CHOICES, "units")


async def _validate(login: str, password: str) -> tuple[str | None, str | None]:
    """Vrátí (číslo měřidla, chybový klíč); přesně jedno z nich je None."""
    client = VhsBenesovClient(login, password)
    try:
        return await client.async_validate(), None
    except InvalidAuth:
        _LOGGER.warning("Přihlášení účtu %s: portál odmítl jméno nebo heslo", login)
        return None, "invalid_auth"
    except VhsError as err:
        _LOGGER.warning("Přihlášení účtu %s selhalo: %s", login, err)
        return None, "cannot_connect"
    finally:
        await client.async_close()


class VhsBenesovConfigFlow(ConfigFlow, domain=DOMAIN):
    """Přihlášení k portálu."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            meter, error = await _validate(
                user_input[CONF_USERNAME].strip(), user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                # Identifikátor je číslo měřidla: stejné měřidlo nejde přidat
                # dvakrát ani pod dvěma přihlášeními. Bez čísla (portál ho
                # neukázal) se použije login.
                await self.async_set_unique_id(meter or user_input[CONF_USERNAME].strip())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=meter or f"Vodoměr {user_input[CONF_USERNAME].strip()}",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options={
                        CONF_IMPORT_HISTORY: bool(user_input[CONF_IMPORT_HISTORY]),
                        CONF_UNITS: user_input[CONF_UNITS],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_UNITS, default=DEFAULT_UNITS): _units_selector(),
                vol.Required(CONF_IMPORT_HISTORY, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nové heslo, když to původní přestalo platit."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            meter, error = await _validate(
                entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            elif meter and entry.unique_id and meter != entry.unique_id:
                # Přihlášení jiného odběrného místa by tiše zaměnilo vodoměr.
                return self.async_abort(reason="wrong_account")
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"login": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> VhsBenesovOptionsFlow:
        return VhsBenesovOptionsFlow()


class VhsBenesovOptionsFlow(OptionsFlow):
    """Počet měsíců pro srovnání s loňskem a jednotky objemu."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Ostatní volby (např. import historie) zůstanou zachované.
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_COMPARE_MONTHS: int(user_input[CONF_COMPARE_MONTHS]),
                    CONF_UNITS: user_input[CONF_UNITS],
                }
            )

        compare = self.config_entry.options.get(CONF_COMPARE_MONTHS, DEFAULT_COMPARE_MONTHS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COMPARE_MONTHS, default=str(compare)
                    ): _compare_selector(),
                    vol.Required(
                        CONF_UNITS,
                        default=self.config_entry.options.get(CONF_UNITS, DEFAULT_UNITS),
                    ): _units_selector(),
                }
            ),
        )
