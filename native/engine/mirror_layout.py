"""Pinned memory layout of the mirror capture probe (native/engine/mirror.h).

Windows X4.exe build 23660954 only (EXECUTABLE in signatures.py). Data only: offsets, vtable
RVAs, class ids and SHA-256 of code ranges whose instructions encode those offsets. No code
bytes are stored. Evidence: reports/REWRITE-ECS-CORE.md (disassembly read by the ecs-core agent,
2026-10-26) and x4-re/coord/reports/entity.md, motion.md.

`layout_sha256()` hashes the canonical text of everything below; the probe writes it into every
capture header, and readers refuse captures made with another table (`require_layout`).
`python native/engine/mirror_layout.py --verify <X4.exe>` re-derives all values from a file copy.
"""
import argparse
import hashlib
import struct
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from native.engine.signatures import EXECUTABLE  # noqa: E402

# Component tree and fields (offsets from the object start).
OFFSETS = {
    'id': 0x08,                 # u64 UniverseID
    'mode': 0x68,               # i32 variant; GetClassID returns the alternate id when != 0
    'parent': 0x70,             # Component* parent
    'children_first': 0xa8,     # child container pointer, walked first (Component::Move)
    'children_second': 0xa0,    # child container pointer, walked second
    'registered': 0xd1,         # u8 (IsValidComponent)
    'death': 0xd3,              # u8 1 dying, 2 dead (KillRecursive)
    'attention': 0x1ec,         # i32
    'controller': 0x200,        # MovementControllerInterface* (positional classes)
    'hull': 0x280,              # f64 (destructible classes)
    'owner_true': 0x348,        # FactionClass* (object classes)
    'owner_cover': 0x350,       # FactionClass*
    'orders_begin': 0x4b0,      # AIOrder** (controllable classes)
    'orders_end': 0x4b8,
    'order_index': 0x4e0,       # u64 current order index
    'container_blocks_begin': 0x08,
    'container_blocks_end': 0x10,
    'container_count': 0x20,    # u32; 0 = empty
    'block_stride': 0x20,
    'block_items_begin': 0x00,
    'block_items_end': 0x08,
    'nav_slots': 0x30,          # pointer to the two pathing state slots
    'nav_slot_stride': 0x1a0,
    'nav_cache': 0x60,          # cached mat4 (16 f32)
    'slot_pose': 0x00,          # mat4 (16 f32)
    'slot_linear': 0x40,        # 4 f32
    'slot_angular': 0x50,       # 4 f32
    'slot_ref': 0xb8,           # u64 reference component id (0 = parent)
    'static_pose': 0x10,        # StaticMovementController mat4
    'faction_id': 0x10,         # MSVC std::string: inline buffer or pointer
    'faction_id_size': 0x20,
    'faction_id_capacity': 0x28,
    'tls_initialized': 0x10,    # u8 in the exe's TLS block (index 0)
    'tls_game_time': 0x300,     # f64 thread game time (GetCurrentGameTime)
}

GLOBALS = {
    'universe': 0x3d198f8,      # U::Universe* (pointer variable)
    'universe_galaxy': 0x228,   # offset of the galaxy pointer in the universe
    'pathing_index': 0x38f4ee4, # u32 current pathing state slot
    'session_seed': 0x3d19860,  # u64 g_SessionSeed (informational, written to the capture header)
}

GETCLASSID_SLOT = 571           # primary vtable slot of GetClassID
TRANSFORM_SLOT = 6              # movement controller vtable slot GetTransform

# (controller kind, primary vtable RVA, slot-6 GetTransform RVA)
CONTROLLERS = [
    ('navigation', 0x2c51158, 0xea5a70),
    ('static', 0x2a34b38, 0x10ba20),
    ('player', 0x2c39488, 0xd8a680),
]

# (name, rva, length, SHA-256): code whose instructions encode the offsets above.
CHECKS = [
    ('component_move', 0x3e4f40, 0x19f, '163cc976b9b2f8ace589e1834de2357babc1c50770c42e791eb94bca466dfbfe'),  # Component::Move: children containers +0xa8 then +0xa0, vfunc +0x1510
    ('container_begin', 0x314fa0, 0xd1, 'c0d7b30d8363132188a28e724eb67c149b6feb48e442f2f1499603b9cf9091b9'),  # child container begin iterator: count +0x20, blocks +0x08/+0x10, stride 0x20
    ('universe_galaxy', 0x9fdf2e, 0x67, 'b83efb628388e007548e747b650e03a3f254252b910e9a0fcf9331df9bf8a83f'),  # UniverseFrameUpdate: g_Universe 0x3d198f8, galaxy at +0x228 (ends before the walk call 0x9fdf95, a walk-agent patch site)
    ('positional_local', 0x7461c0, 0x60, 'bce7e2c7291aa425f2c8fe854c67b4fbe37c257ca2b3a22d27a077c95c98cce9'),  # Positional::GetLocalTransform: controller +0x200, parent +0x70
    ('nav_transform_head', 0xea5a90, 0x60, '8645c300828866fd3e9d8ad4ef92b1bad5cdfa0ef5784f2ceeb73efc1dd307b1'),  # NavigationController::GetTransformImpl head: slot pointer +0x30
    ('nav_transform_slot', 0xea5c10, 0x38, 'a2684777b432a20e08542f080a16f0be68974f0a44a882b64de7891114d80ee6'),  # GetTransformImpl: g_PathingStateBufferIndex 0x38f4ee4, stride 0x1a0, ref +0xb8
    ('static_transform', 0x10ba20, 0x25, '59916bde73b664f995fc0abd6ec35bd3a27996e03eed8b116f8575cde9c1df04'),  # StaticMovementController slot 6: stored transform +0x10..+0x4f
    ('hull_percent', 0xf0a40, 0x50, 'b46d09276f4a3bb7afde4a0543d0d69f02d4c4d0a7eb42f1dca0f9b4b97d196b'),  # Destructible::GetHullPercent: hull double +0x280
    ('owner_true', 0x352b10, 0x8, '7900022f876803a52ef0ebfca5f0f9ae79603c127b6ad4a32bc834758e27be53'),  # Object::GetTrueOwner: +0x348
    ('owner_cover', 0x352b20, 0x8, 'e02fe4e1f12c704bd82cfb59bf677358672b792cf0c660d3ee77e3aa2b94a38c'),  # Object::GetCoverOwner: +0x350
    ('num_orders', 0x1f7370, 0x56, 'e96c514dd7a357a8a5dfdffdf374edc85036ad7cdfb617821ad811436bfccaf1'),  # GetNumOrders: order vector +0x4b0..+0x4b8
    ('order_index', 0x1f8440, 0x50, '375dbb46a398c3b79dce806543573ed417634ad8c060af217e67ceb9a265eaf3'),  # GetOrderQueueCurrentIdx: +0x4e0
    ('set_attention', 0x3d5660, 0x92, 'd9f314cdfce8e24e3b26f5d5023181c3c147aef10093fb1888a1db303713b678'),  # Component::SetAttention: +0x1ec
    ('is_valid', 0xb002e0, 0x2f, '9af3dac63c24be521f8606fc9e95cb8dd6c35ce56afd64acf8b2b53eed8e76c5'),  # IsValidComponent: registered byte +0xd1
    ('death_dying', 0x3e5298, 0x10, 'dd07ea3b6b70f0bd70d15b93bf5ae509d6f124f05ba8b6c9e0e5323e4814f658'),  # KillRecursive: death state +0xd3 = 1
    ('death_dead', 0x3e5468, 0x10, '4c2ed6ecb0b2aac4ad7171be8e045eb10afc063da5b16335219bda4ce3d74b1d'),  # KillRecursive: death state +0xd3 = 2
    ('game_time_tls', 0xafcdc0, 0x32, 'd2f37b9506540dc4aa77168e58bbf4e4bfefd5c6692059a8343027af81c328cd'),  # GetCurrentGameTime: TLS block (index 0) +0x10 init flag, +0x300 game time
    ('session_seed', 0x34faf0, 0x8, '9ed91addd695e5db89aff28713b8319294ecbb5d66788d6d148b4eb83f1a4077'),  # session seed getter: u64 g_SessionSeed 0x3d19860
]

# Every U::Component subclass (RTTI): primary vtable RVA, class id when Component+0x68 == 0,
# class id otherwise (GetClassID reads +0x68), the GetClassID function RVA, base-class flags.
CLASSES = [
    (0x2a8adf8, 0x19, 0x57, 0x357040, 'positional', 'U::ControlRoom'),
    (0x2a8ce88, 0x70, 0x49, 0x353f60, 'positional,destructible,object,controllable', 'U::Controllable'),
    (0x2a8f1f0, 0x6f, 0x49, 0x355a50, 'positional,destructible,object,controllable,defensible,container', 'U::Container'),
    (0x2a91750, 0x16, 0x16, 0x19b5d0, '', 'U::Component'),
    (0x2a932b8, 0x13, 0x49, 0x3cb850, 'positional,destructible,object', 'U::CollectableData'),
    (0x2a95528, 0x15, 0x49, 0x3cb700, 'positional,destructible,object', 'U::CollectableWares'),
    (0x2a97908, 0x14, 0x49, 0x3cb7b0, 'positional,destructible,object', 'U::CollectableBlueprints'),
    (0x2a99cd0, 0x11, 0x49, 0x3cb5d0, 'positional,destructible,object', 'U::CollectableShieldRestore'),
    (0x2a9bff0, 0x12, 0x49, 0x3cb660, 'positional,destructible,object', 'U::CollectableAmmo'),
    (0x2a9e2f8, 0x6e, 0x49, 0x3cb470, 'positional,destructible,object', 'U::Collectable'),
    (0x2aa0628, 0x10, 0x57, 0x3734a0, 'positional', 'U::Cockpit'),
    (0x2aa2498, 0x0f, 0x0f, 0x198090, 'positional,space', 'U::Cluster'),
    (0x2aa4558, 0x0e, 0x49, 0x3b1eb0, 'positional,destructible,object', 'U::Checkpoint'),
    (0x2aa69e8, 0x0c, 0x24, 0x3b0bb0, 'positional,destructible', 'U::CargoBay'),
    (0x2aa89d8, 0x09, 0x24, 0x354a40, 'positional,destructible', 'U::BuildModule'),
    (0x2aaa9d8, 0x0a, 0x16, 0x3548e0, '', 'U::BuildProcessor'),
    (0x2aaca18, 0x08, 0x49, 0x383ec0, 'positional,destructible,object,controllable,defensible,container', 'U::BuildStorage'),
    (0x2aaefb8, 0x1f, 0x24, 0x36fad0, 'positional,destructible', 'U::Crystal'),
    (0x2ab1130, 0x05, 0x24, 0x36fa30, 'positional,destructible', 'U::Attachment'),
    (0x2ab3148, 0x04, 0x49, 0x353200, 'positional,destructible,object', 'U::Asteroid'),
    (0x2ab5378, 0x41, 0x24, 0x353170, 'positional,destructible', 'U::MiningNode'),
    (0x2ab7438, 0x03, 0x49, 0x369670, 'positional,destructible,object', 'U::Anomaly'),
    (0x2abf5e8, 0x00, 0x4d, 0x34fec0, 'positional', 'U::Accessory'),
    (0x2acadc0, 0x26, 0x16, 0x4be990, '', 'U::DismantleProcessor'),
    (0x2acca90, 0x51, 0x49, 0x4be840, 'positional,destructible,object', 'U::Recyclable'),
    (0x2acec88, 0x4e, 0x24, 0x4bd180, 'positional,destructible', 'U::ProcessingModule'),
    (0x2ad0ff8, 0x71, 0x49, 0x4bc870, 'positional,destructible,object', 'U::Explosive'),
    (0x2ad3458, 0x2c, 0x2c, 0x4b75b0, 'positional', 'U::Entity'),
    (0x2ad58f8, 0x2b, 0x24, 0x4bab40, 'positional,destructible', 'U::Engine'),
    (0x2ad7a88, 0x29, 0x49, 0x4be680, 'positional,destructible,object', 'U::Drop'),
    (0x2ad9ca0, 0x28, 0x57, 0x4bce60, 'positional', 'U::DockingBay'),
    (0x2adbdc0, 0x27, 0x24, 0x4bd0a0, 'positional,destructible', 'U::DockArea'),
    (0x2adde00, 0x25, 0x24, 0x4be7a0, 'positional,destructible', 'U::Detector'),
    (0x2adfec8, 0x24, 0x24, 0x4ba920, 'positional,destructible', 'U::Destructible'),
    (0x2ae1fc0, 0x23, 0x49, 0x4bd310, 'positional,destructible,object,controllable,defensible', 'U::Defensible'),
    (0x2ae50d0, 0x21, 0x49, 0x4bbd60, 'positional,destructible,object', 'U::DataVault'),
    (0x2ae7450, 0x20, 0x57, 0x4b72a0, 'positional', 'U::CutsceneAnchor'),
    (0x2af5ce0, 0x77, 0x49, 0x5be930, 'positional,destructible,object', 'U::TriggerObject'),
    (0x2af7fa8, 0x5f, 0x24, 0x650780, 'positional,destructible', 'U::SignalLeak'),
    (0x2af9e88, 0x40, 0x49, 0x64db60, 'positional,destructible,object', 'U::Mine'),
    (0x2afc1f8, 0x01, 0x24, 0x5ea240, 'positional,destructible', 'U::AdSign'),
    (0x2afe0b8, 0x02, 0x49, 0x5ea2d0, 'positional,destructible,object', 'U::AdSignObject'),
    (0x2b00620, 0x44, 0x24, 0x60a830, 'positional,destructible', 'U::MissileTurret'),
    (0x2b02988, 0x43, 0x24, 0x6091b0, 'positional,destructible', 'U::MissileLauncher'),
    (0x2b04ba8, 0x42, 0x49, 0x5ea170, 'positional,destructible,object', 'U::Missile'),
    (0x2b06fb0, 0x3c, 0x49, 0x605590, 'positional,destructible,object', 'U::LandmarkObject'),
    (0x2b091e0, 0x33, 0x33, 0x4b69c0, 'positional,space', 'U::Highway'),
    (0x2b0b398, 0x38, 0x4d, 0x5bfc70, 'positional', 'U::HighwayScene'),
    (0x2b0d238, 0x39, 0x24, 0x5bfa50, 'positional,destructible', 'U::HighwayTrigger'),
    (0x2b0f198, 0x34, 0x24, 0x5bfaf0, 'positional,destructible', 'U::HighwayBlocker'),
    (0x2b11168, 0x35, 0x24, 0x5bfb80, 'positional,destructible', 'U::HighwayBooster'),
    (0x2b13b50, 0x30, 0x49, 0x5bea50, 'positional,destructible,object', 'U::Gate'),
    (0x2b15e38, 0x2f, 0x2f, 0x5be570, 'positional,space', 'U::Galaxy'),
    (0x2b19760, 0x65, 0x24, 0x792a40, 'positional,destructible', 'U::Switchable'),
    (0x2b1b848, 0x59, 0x24, 0x6a04a0, 'positional,destructible', 'U::ShieldGenerator'),
    (0x2b1ea48, 0x58, 0x58, 0x6a22a0, 'positional,space', 'U::Sector'),
    (0x2b20990, 0x57, 0x57, 0x6a0e00, 'positional', 'U::Scene'),
    (0x2b22880, 0x55, 0x49, 0x6f7e90, 'positional,destructible,object', 'U::Satellite'),
    (0x2b24b80, 0x56, 0x24, 0x6f7d00, 'positional,destructible', 'U::Scanner'),
    (0x2b26a78, 0x54, 0x57, 0x6a0ed0, 'positional', 'U::Room'),
    (0x2b288b0, 0x53, 0x49, 0x6f7f30, 'positional,destructible,object', 'U::ResourceProbe'),
    (0x2b2ad60, 0x52, 0x4d, 0x6c9d40, 'positional', 'U::Region'),
    (0x2b2cae8, 0x50, 0x24, 0x6a14a0, 'positional,destructible', 'U::Radar'),
    (0x2b2e9c0, 0x4f, 0x24, 0x6a11f0, 'positional,destructible', 'U::ProductionModule'),
    (0x2b30a08, 0x4d, 0x4d, 0x69fe40, 'positional', 'U::Positional'),
    (0x2b329a8, 0x4c, 0x4c, 0x6a1d00, 'positional', 'U::Player'),
    (0x2b36658, 0x49, 0x49, 0x6a0800, 'positional,destructible,object', 'U::Object'),
    (0x2b38a48, 0x48, 0x48, 0x6a0c10, 'positional', 'U::NPC'),
    (0x2b3ac58, 0x74, 0x74, 0x6a0160, 'positional', 'U::NonPlayer'),
    (0x2b3cf48, 0x46, 0x49, 0x6b6100, 'positional,destructible,object', 'U::NavBeacon'),
    (0x2b3f228, 0x47, 0x24, 0x6b7070, 'positional,destructible', 'U::NavContext'),
    (0x2b41108, 0x73, 0x24, 0x6a1070, 'positional,destructible', 'U::Module'),
    (0x2b43330, 0x31, 0x24, 0x6a1150, 'positional,destructible', 'U::HabitationModule'),
    (0x2b462c8, 0x17, 0x17, 0x3697e0, 'positional', 'U::Computer'),
    (0x2b4f8a0, 0x2d, 0x24, 0x81feb0, 'positional,destructible', 'U::ForceEmitter'),
    (0x2b518c8, 0x22, 0x24, 0x7c7740, 'positional,destructible', 'U::DefenceModule'),
    (0x2b53918, 0x6a, 0x24, 0x81ff60, 'positional,destructible', 'U::VenturePlatform'),
    (0x2b55968, 0x6c, 0x24, 0x7c77e0, 'positional,destructible', 'U::WelfareModule'),
    (0x2b57900, 0x4a, 0x24, 0x7c7880, 'positional,destructible', 'U::Pier'),
    (0x2b59950, 0x18, 0x24, 0x7c76b0, 'positional,destructible', 'U::ConnectionModule'),
    (0x2b5bb40, 0x3e, 0x24, 0x7eb5d0, 'positional,destructible', 'U::Lock'),
    (0x2b5dab8, 0x3f, 0x49, 0x7eb670, 'positional,destructible,object', 'U::Lockbox'),
    (0x2b5fce8, 0x07, 0x24, 0x812370, 'positional,destructible', 'U::BombLauncher'),
    (0x2b61f30, 0x1b, 0x4d, 0x81fdf0, 'positional', 'U::Crate'),
    (0x2b63c18, 0x06, 0x49, 0x812280, 'positional,destructible,object', 'U::Bomb'),
    (0x2b66080, 0x6d, 0x6d, 0x7c7cd0, 'positional,space', 'U::Zone'),
    (0x2b68278, 0x6b, 0x24, 0x7c71c0, 'positional,destructible', 'U::Weapon'),
    (0x2b6a740, 0x78, 0x24, 0x7c7620, 'positional,destructible', 'U::WalkableModule'),
    (0x2b6cc38, 0x69, 0x4d, 0x80d000, 'positional', 'U::UIElement'),
    (0x2b6e910, 0x68, 0x24, 0x7c7580, 'positional,destructible', 'U::Turret'),
    (0x2b70ba0, 0x67, 0x4d, 0x80a480, 'positional', 'U::TextDisplay'),
    (0x2b72a40, 0x66, 0x16, 0x806ea0, '', 'U::TargetPoint'),
    (0x2b74548, 0x63, 0x24, 0x7c79c0, 'positional,destructible', 'U::StorageModule'),
    (0x2b764d8, 0x62, 0x49, 0x7eb270, 'positional,destructible,object,controllable,defensible,station,container', 'U::Station'),
    (0x2b789b0, 0x60, 0x49, 0x7c9c10, 'positional,destructible,object,controllable,defensible,ship,container', 'U::SpaceSuit'),
    (0x2b7afe8, 0x76, 0x76, 0x7c7ba0, 'positional,space', 'U::Space'),
    (0x2b7d738, 0x5a, 0x49, 0x7c9010, 'positional,destructible,object,controllable,defensible,ship,container', 'U::ShipExtraSmall'),
    (0x2b7feb8, 0x5b, 0x49, 0x7c9140, 'positional,destructible,object,controllable,defensible,ship,container', 'U::ShipSmall'),
    (0x2b82440, 0x5c, 0x49, 0x7c9250, 'positional,destructible,object,controllable,defensible,ship,container', 'U::ShipMedium'),
    (0x2b84a98, 0x75, 0x49, 0x7c8480, 'positional,destructible,object,controllable,defensible,ship,container', 'U::Ship'),
    (0x2b902a0, 0x1c, 0x4d, 0x8d3d50, 'positional', 'U::CrateLarge'),
    (0x2b91fe0, 0x1d, 0x4d, 0x8d3ca0, 'positional', 'U::CrateMedium'),
    (0x2b93d20, 0x1e, 0x4d, 0x8d3bf0, 'positional', 'U::CrateSmall'),
    (0x2b99a88, 0x5e, 0x49, 0x870d40, 'positional,destructible,object,controllable,defensible,ship,container', 'U::ShipExtraLarge'),
    (0x2b9bff8, 0x5d, 0x49, 0x870ba0, 'positional,destructible,object,controllable,defensible,ship,container', 'U::ShipLarge'),
    (0x2ba3240, 0x3b, 0x49, 0x991820, 'positional,destructible,object', 'U::InfluenceObject'),
    (0x2c21248, 0x61, 0x4d, 0xd4da70, 'positional', 'U::Stardust'),
    (0x2c23018, 0x64, 0x4d, 0xe438e0, 'positional', 'U::Sun'),
    (0x2c24dd8, 0x4b, 0x4d, 0xe37190, 'positional', 'U::Planet'),
    (0x2c26b88, 0x45, 0x4d, 0xe35b30, 'positional', 'U::Moon'),
    (0x2c2a1a8, 0x3d, 0x4d, 0xe23310, 'positional', 'U::LensFlare'),
    (0x2c2cee0, 0x3a, 0x57, 0xd64770, 'positional', 'U::HoloMap'),
    (0x2c2ff40, 0x37, 0x49, 0xd834c0, 'positional,destructible,object', 'U::HighwayExitGate'),
    (0x2c321d8, 0x2e, 0x4d, 0xd9d1d0, 'positional', 'U::FogVolume'),
    (0x2c34050, 0x36, 0x49, 0xd83430, 'positional,destructible,object', 'U::HighwayEntryGate'),
    (0x2c36278, 0x32, 0x24, 0xd9ef60, 'positional,destructible', 'U::HackerProbe'),
    (0x2c39778, 0x2a, 0x4d, 0xd4db10, 'positional', 'U::EffectObject'),
    (0x2c3c598, 0x1a, 0x4d, 0xd6d4b0, 'positional', 'U::Countermeasure'),
    (0x2c40d18, 0x0d, 0x4d, 0xd547b0, 'positional', 'U::CelestialBody'),
    (0x2c42ac8, 0x0b, 0x4d, 0xd49820, 'positional', 'U::Bullet'),
]
CLASSID_CODE_SHA256 = '605b94b4829797da30e72ccc516851291b0d4dccf89da3ec3f1c713f5d539ad0'

FLAG_BITS = {'positional': 1, 'destructible': 2, 'object': 4, 'controllable': 8, 'defensible': 16,
             'ship': 32, 'station': 64, 'space': 128, 'container': 256}
CLASSID_CODE_LENGTH = 16


def flag_value(flags: str) -> int:
    return sum(FLAG_BITS[f] for f in flags.split(',') if f)


def layout_text() -> str:
    """Canonical text of the whole table (input of the layout hash)."""
    lines = [f"exe {EXECUTABLE['sha256']} {EXECUTABLE['timestamp']:#x} {EXECUTABLE['image_size']:#x}"]
    lines += [f"off {k} {v:#x}" for k, v in sorted(OFFSETS.items())]
    lines += [f"global {k} {v:#x}" for k, v in sorted(GLOBALS.items())]
    lines += [f"slot getclassid {GETCLASSID_SLOT} transform {TRANSFORM_SLOT}"]
    lines += [f"ctrl {k} {vt:#x} {fn:#x}" for k, vt, fn in CONTROLLERS]
    lines += [f"check {n} {r:#x} {l:#x} {s}" for n, r, l, s in CHECKS]
    lines += [f"class {vt:#x} {a:#x} {b:#x} {fn:#x} {flag_value(f)}" for vt, a, b, fn, f, _ in CLASSES]
    lines += [f"classcode {CLASSID_CODE_LENGTH} {CLASSID_CODE_SHA256}"]
    return '\n'.join(lines) + '\n'


def layout_sha256() -> str:
    return hashlib.sha256(layout_text().encode()).hexdigest()


def c_header() -> str:
    digest = bytes.fromhex(layout_sha256())
    out = ['#pragma once', '/* Generated by native/engine/mirror_layout.py; do not edit. */']
    out.append(f'#define MIRROR_LAYOUT_SHA256 "{layout_sha256()}"')
    out.append('static const unsigned char mirror_layout_digest[32] = {' + ','.join(f'0x{b:02x}' for b in digest) + '};')
    for k, v in OFFSETS.items():
        out.append(f'#define MO_{k.upper()} 0x{v:x}u')
    for k, v in GLOBALS.items():
        out.append(f'#define MG_{k.upper()} 0x{v:x}u')
    out.append(f'#define MIRROR_GETCLASSID_SLOT {GETCLASSID_SLOT}u')
    out.append(f'#define MIRROR_TRANSFORM_SLOT {TRANSFORM_SLOT}u')
    for name, bit in FLAG_BITS.items():
        out.append(f'#define MF_{name.upper()} {bit}u')
    for k, vt, fn in CONTROLLERS:
        out.append(f'#define MC_{k.upper()}_VTABLE 0x{vt:x}u')
        out.append(f'#define MC_{k.upper()}_TRANSFORM 0x{fn:x}u')
    out.append('typedef struct { const char *name; size_t rva; ULONG length; const char *sha; } mirror_check;')
    out.append('static const mirror_check mirror_checks[] = {')
    for n, r, l, s in CHECKS:
        out.append(f'    {{"{n}", 0x{r:x}, 0x{l:x}, "{s}"}},')
    out.append('};')
    out.append('typedef struct { uint32_t vtable; uint8_t id0, id1; uint16_t flags; uint32_t classid_fn; } mirror_class;')
    out.append('/* sorted by vtable RVA (binary search) */')
    out.append('static const mirror_class mirror_classes[] = {')
    for vt, a, b, fn, f, name in sorted(CLASSES):
        out.append(f'    {{0x{vt:x}, 0x{a:02x}, 0x{b:02x}, 0x{flag_value(f):x}, 0x{fn:x}}}, /* {name} */')
    out.append('};')
    out.append(f'#define MIRROR_CLASS_COUNT {len(CLASSES)}u')
    out.append(f'#define MIRROR_CLASSID_CODE_LENGTH {CLASSID_CODE_LENGTH}u')
    out.append(f'#define MIRROR_CLASSID_CODE_SHA256 "{CLASSID_CODE_SHA256}"')
    return '\n'.join(out) + '\n'


# --- offline verification against a copy of X4.exe ---------------------------------------

class PeFile:
    def __init__(self, path: Path):
        self.data = path.read_bytes()
        e_lfanew = struct.unpack_from('<I', self.data, 0x3c)[0]
        nsec = struct.unpack_from('<H', self.data, e_lfanew + 6)[0]
        opt = struct.unpack_from('<H', self.data, e_lfanew + 20)[0]
        self.timestamp = struct.unpack_from('<I', self.data, e_lfanew + 8)[0]
        self.image_base = struct.unpack_from('<Q', self.data, e_lfanew + 24 + 24)[0]
        self.image_size = struct.unpack_from('<I', self.data, e_lfanew + 24 + 56)[0]
        self.sections = []
        base = e_lfanew + 24 + opt
        for i in range(nsec):
            vsize, va, rsize, rptr = struct.unpack_from('<IIII', self.data, base + 40 * i + 8)
            self.sections.append((va, max(vsize, rsize), rptr, rsize))

    def read(self, rva: int, n: int) -> bytes:
        for va, size, rptr, rsize in self.sections:
            if va <= rva and rva + n <= va + size:
                off = rva - va
                if off + n > rsize:
                    raise ValueError(f'rva {rva:#x} not in file data')
                return self.data[rptr + off:rptr + off + n]
        raise ValueError(f'rva {rva:#x} not in a section')


def class_ids_from_code(code: bytes):
    """The four GetClassID shapes of this build (byte patterns, not disassembly)."""
    if code[0] == 0xb8 and code[5] == 0xc3:                       # mov eax, imm32; ret
        v = struct.unpack_from('<I', code, 1)[0]
        return v, v
    if code[:4] == bytes([0x83, 0x79, 0x68, 0x00]) and code[4] == 0xb8 and code[9] == 0xba:
        b = struct.unpack_from('<I', code, 5)[0]                   # cmp [rcx+0x68],0; mov eax,B; mov edx,A; cmove
        a = struct.unpack_from('<I', code, 10)[0]
        return a, b
    if code[:3] == bytes([0x8b, 0x41, 0x68]) and code[3:5] == bytes([0xf7, 0xd8]) and code[5:7] == bytes([0x1b, 0xc0]):
        if code[7] == 0x83 and code[8] == 0xe0:                    # and eax, imm8 -> 0 / K
            return 0, code[9]
        if code[7] == 0x83 and code[8] == 0xc0:                    # add eax, imm8 -> K / K-1
            return code[9], code[9] - 1
        raise ValueError(f'unknown neg/sbb form {code.hex()}')
    raise ValueError(f'unknown GetClassID shape {code.hex()}')


def verify(exe: Path) -> list:
    problems = []
    pe = PeFile(exe)
    if hashlib.sha256(pe.data).hexdigest() != EXECUTABLE['sha256']:
        problems.append('executable SHA-256 differs from the pinned build')
    for name, rva, length, sha in CHECKS:
        if hashlib.sha256(pe.read(rva, length)).hexdigest() != sha:
            problems.append(f'check {name} at {rva:#x} differs')
    code = b''
    for vt, a, b, fn, _flags, name in CLASSES:
        slot = struct.unpack('<Q', pe.read(vt + GETCLASSID_SLOT * 8, 8))[0] - pe.image_base
        if slot != fn:
            problems.append(f'{name}: vtable slot {GETCLASSID_SLOT} is {slot:#x}, expected {fn:#x}')
        chunk = pe.read(fn, CLASSID_CODE_LENGTH)
        code += chunk
        try:
            if class_ids_from_code(chunk) != (a, b):
                problems.append(f'{name}: class ids {class_ids_from_code(chunk)} expected {(a, b)}')
        except ValueError as e:
            problems.append(f'{name}: {e}')
    if hashlib.sha256(code).hexdigest() != CLASSID_CODE_SHA256:
        problems.append('combined GetClassID code hash differs')
    for kind, vt, fn in CONTROLLERS:
        slot = struct.unpack('<Q', pe.read(vt + TRANSFORM_SLOT * 8, 8))[0] - pe.image_base
        if slot != fn:
            problems.append(f'controller {kind}: slot {TRANSFORM_SLOT} is {slot:#x}, expected {fn:#x}')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--verify', type=Path, help='copy of X4.exe to re-derive every pinned value from')
    ap.add_argument('--header', action='store_true', help='print the generated C header')
    args = ap.parse_args()
    print(f'layout sha256 {layout_sha256()} ({len(CLASSES)} classes, {len(CHECKS)} code checks)')
    if args.header:
        print(c_header())
    if args.verify:
        problems = verify(args.verify)
        for p in problems:
            print('FAIL', p)
        print('verify:', 'ok' if not problems else f'{len(problems)} problem(s)')
        sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
