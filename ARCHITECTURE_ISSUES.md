# PyWarema Architecture Issues & Race Conditions

## Problem: Recurring Race Conditions

### Current Design (BROKEN)
```
WmsStick (Background Thread)
  └─> _update_blind_pos()
      └─> callback() [FIRES FROM THREAD!]
          └─> Dispatcher Signal
              └─> (Entität muss bereits registriert sein)

Entität (async_added_to_hass)
  └─> async_dispatcher_connect()
      └─ RACE: Signal könnte schon gesendet sein!
```

**Das Kernproblem:**
- Der Stick hat den **State** (Position im `blind.pos_current`)
- Aber der Coordinator **gibt ihn nicht direkt weiter**
- Entitäten verlassen sich nur auf **Signals**
- Wenn Signal vor Registrierung ankommt → **verpasst**

### Warum passiert das ständig?

1. **Position-Abfrage Reihenfolge:**
   - `async_connect()` startet Position-Abfragen
   - Platforms werden dann registriert
   - Antworten kommen mit ~100ms Verzögerung
   - Aber Platforms brauchen auch Zeit zum Setup

2. **Timing-Fenster zu klein:**
   - Stick hat Position (pos_current gespeichert)
   - Coordinator sendet Signal
   - Entität versucht sich zu registrieren
   - Zu spät! Signal ist vorbei

3. **Kein zentraler "Source of Truth":**
   - Position ist verteilt (im Blind-Objekt, im Signal, nirgendwo gecacht)
   - Entitäten wissen nicht, wo sie die aktuelle Position holen sollen

---

## Besseres Design: HA-natives `DataUpdateCoordinator` Pattern

> ⚠️ **Wichtig:** Mein erster Vorschlag (handgeschriebenes "Observer Pattern mit
> State-Caching") war im **Prinzip korrekt**, aber die **falsche Umsetzung**.
> Home Assistant bietet **genau dieses Pattern bereits eingebaut** an:
> `DataUpdateCoordinator` + `CoordinatorEntity`.
> Quelle: https://developers.home-assistant.io/docs/integration_fetching_data

### Warum DataUpdateCoordinator statt selbst gebaut?

HA-Doku, Originalzitat:
> *"a single periodical poll on this endpoint, and then let entities know as
> soon as new data is available for them."*

Das ist **exakt** unser Use-Case. Der Coordinator:
- speichert State zentral in `self.data` (= unser "State Cache")
- entities lesen `self.coordinator.data[snr]` **on-demand** in ihren Properties
- → **Race Condition unmöglich**, weil Daten nicht "verpasst" werden können
- liefert `available` automatisch aus `last_update_success`
- `CoordinatorEntity` kümmert sich um Subscribe/Unsubscribe automatisch

### Push vs. Poll — Warema ist PUSH

Der WMS-Stick hat **eigene Polling-Threads** (`set_pos_upd_interval`) und feuert
Callbacks. Aus HA-Sicht ist das ein **Push-Integration**. HA-Doku dazu:

> *"For push endpoints, avoid passing `update_method` and `update_interval`.
> When new data arrives, call `coordinator.async_set_updated_data(data)`."*

→ Also: **KEIN** `update_interval` setzen. Stattdessen im Stick-Callback
`async_set_updated_data()` aufrufen.

### Neuer Coordinator

```python
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

class WaremaCoordinator(DataUpdateCoordinator[dict[int, BlindState]]):
    def __init__(self, hass, entry):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # KEIN update_interval → reines Push (Stick pollt selbst)
        )
        self.entry = entry
        self.stick = None

    def _wms_callback(self, error, msg):
        """Läuft im Stick-Background-Thread."""
        if not msg or msg.get("topic") != TOPIC_BLIND_POSITION_UPDATE:
            return
        p = msg["payload"]
        snr = p["snr"]

        # Neues data-dict bauen (bestehende States kopieren + diesen updaten)
        new_data = dict(self.data or {})
        new_data[snr] = BlindState(
            snr=snr,
            snr_hex=p["snr_hex"],
            position=p["position"],
            angle=p["angle"],
            moving=p["moving"],
        )

        # WICHTIG: threadsafe auf den Event-Loop bringen
        self.hass.loop.call_soon_threadsafe(
            self.async_set_updated_data, new_data
        )
```

### Neue Entität (z.B. Sensor)

```python
from homeassistant.helpers.update_coordinator import CoordinatorEntity

class WaremaWmsPositionSensor(CoordinatorEntity[WaremaCoordinator], SensorEntity):
    def __init__(self, coordinator, snr, snr_hex, ...):
        super().__init__(coordinator, context=snr)   # ← HA verbindet Subscribe automatisch
        self._snr = snr
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_position"

    @property
    def native_value(self) -> int | None:
        # Liest IMMER den aktuellen State on-demand → KEINE Race Condition
        state = self.coordinator.data.get(self._snr) if self.coordinator.data else None
        return state.position if state else None

    # Optional: nur nötig, wenn lokale Attribute gecacht werden sollen.
    # Bei reinem coordinator.data-Lesen in Properties NICHT zwingend.
    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
```

**Der entscheidende Unterschied:** Es gibt **kein** `async_added_to_hass` mit
manueller `dispatcher_connect`-Registrierung mehr. `CoordinatorEntity` macht das
selbst. Und weil Properties `coordinator.data` **bei jeder Abfrage** lesen, gibt
es kein Zeitfenster, in dem ein Update "verpasst" werden kann.

---

## Warum das alle Race Conditions eliminiert

| Szenario | Altes Design (Dispatcher) | DataUpdateCoordinator |
|----------|---------------------------|------------------------|
| Update kommt VOR Entity-Setup | ❌ verpasst | ✅ liegt in `coordinator.data`, wird beim nächsten Property-Read gelesen |
| Mehrere Platforms, unterschiedl. Timing | ❌ jede muss selbst initialisieren | ✅ alle lesen denselben `coordinator.data` |
| Entity-Reload | ❌ erneute manuelle Abfrage nötig | ✅ State sofort da |

---

## Implementierungs-Roadmap

### Phase 1: Coordinator auf DataUpdateCoordinator umstellen (~30 min)
- [ ] `BlindState` Dataclass (frozen, mit `__eq__`) definieren
- [ ] `WaremaCoordinator(DataUpdateCoordinator)` erben lassen
- [ ] `_wms_callback` → `async_set_updated_data()` (threadsafe)
- [ ] **KEIN** `update_interval` (Stick pollt selbst)
- [ ] Initiale Daten in `async_connect` per `async_set_updated_data({})` setzen

### Phase 2: Entitäten auf CoordinatorEntity migrieren (~1 h)
- [ ] `cover.py`: `CoordinatorEntity`, Properties lesen `coordinator.data[snr]`
- [ ] `sensor.py`: dito
- [ ] `binary_sensor.py`: dito
- [ ] Alle `async_dispatcher_connect` + manuelle `get_position`-Hacks entfernen

### Phase 3: Aufräumen
- [ ] `SIGNAL_POSITION_UPDATE` löschen
- [ ] `async_added_to_hass`-Workarounds löschen
- [ ] Race-Condition-Logging entfernen

---

## Hinweise

### `always_update` (HA 2023.6+)
`DataUpdateCoordinator` schreibt per Default State auch bei unveränderten Daten.
Mit `always_update=False` + `__eq__` auf `BlindState` werden redundante
State-Writes vermieden (Performance bei häufigem Polling).

### Threadsafe-Bridge bleibt nötig
Der Stick-Callback läuft im Background-Thread. `async_set_updated_data` MUSS
über `hass.loop.call_soon_threadsafe(...)` auf den Event-Loop gebracht werden —
genau wie aktuell beim Dispatcher.

---

## Zusammenfassung

| Aspekt | Aktuell (Dispatcher) | DataUpdateCoordinator (HA-Standard) |
|--------|----------------------|--------------------------------------|
| **State Management** | Verteilt, kein Cache | Zentral in `coordinator.data` |
| **Race Conditions** | ❌ Häufig | ✅ Unmöglich (on-demand Read) |
| **Subscribe/Unsubscribe** | Manuell | Automatisch via `CoordinatorEntity` |
| **`available`** | Manuell | Automatisch (`last_update_success`) |
| **Code-Menge** | Viel Boilerplate | Minimal |
| **HA-Konform** | ❌ Nein | ✅ Offizielle Empfehlung |

