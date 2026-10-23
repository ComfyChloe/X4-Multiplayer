# X4 Multiplayer — source inspection

This repository presents an experimental X4 Foundations multiplayer source
snapshot corresponding to internal version **{{VERSION}}**. You may read the code
and download an unchanged copy for private source inspection under
[LICENSE.txt](LICENSE.txt). This is **not an open-source license**.

The repository is a public source mirror. Visitors have no write access to the
maintained original. GitHub's viewing/forking rights still apply; modification,
redistribution and other reuse require separate permission where not otherwise
permitted by applicable law or GitHub's terms. See [repository policy](POLICY.md).

## What this snapshot implements

- A host-generated native starting save, authenticated transfer, validation and
  role admission so both players begin from the same initial world.
- Host NPC scene replication, player motion, ordinary cooperative cargo,
  shared display identities and the custom Cooperative Trial mission.
- Windows/Linux launcher and relay source, with isolated test-profile guards.

This is a work in progress. Ongoing station economy and construction/destruction,
full NPC/combat/docking authority, campaign save/resume, catching up absent players,
ordinary shared missions and ingame chat are unfinished. A shared starting world
is not a continuously synchronized universe. Read [STATUS.md](STATUS.md) for the
observed results and their limits.

## Read the implementation

[CODE-MAP.md](CODE-MAP.md) links the main components. Production source is retained
byte-for-byte from the recorded source commit. A small number of test fixtures use
synthetic addresses/profile IDs in place of internal examples; those substitutions
are recorded in [SOURCE-MANIFEST.json](SOURCE-MANIFEST.json), together with hashes.

This is not an installable mod package or a public playtest release. Compiled
libraries, Python runtimes, game assets/schemas, saves, session credentials,
internal reports and private development history are intentionally absent.
Distribution scripts and historical package instructions are included for code
inspection; their presence is not installation guidance or permission to run,
build or redistribute the software. Package builders depend on internal release
reports and runtime inputs that are not supplied here.

X4 Foundations and related names belong to their respective owners. This is an
independent experimental project; no official endorsement is claimed. See
[third-party scope](THIRD-PARTY.txt). Do not upload saves, credentials or personal
logs when discussing this snapshot.
