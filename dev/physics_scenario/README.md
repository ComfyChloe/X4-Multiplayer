# Disposable ForcedFlight capture fixture

Use only as an extra extension in a released `x4lane run` on a disposable lane.
Never install in the normal game. It is not included in production packages.

In a new custom_creative world, after15 game seconds, create3 player-owned
Argon fighters with pilots and differing initial positions/orientations. Each
pilot runs an original short script selecting the engine's supported forced
flight-control model for60 seconds, then restores the default model. At90
seconds the MD fixture removes its ships. The player ship/settings are unchanged.

Pair with `X4LAN_FLIGHTCAP='forced'`, a fresh capture path, enough frames to
reach the fixture, and checks for PHYSICSSCN spawned/forced and FLIGHTCAP written.
The model's default target may remain stationary. Report distinct inputs and
outputs; this fixture is not general flight/AI acceptance.
