"""Build-pinned X4 signatures; hashes only, never executable bytes."""

EXECUTABLE = {
    'size': 55412768,
    'sha256': '19750a6563889a970f434b5566eb396c6b2dc29ff814bd3e336f838176ad6891',
    'timestamp': 0x6A1EADED,
    'image_size': 0x6FA9000,
}

COMMON_SITES = {
    'move_worker_run': (0xEF4400, 25, '4449b89ae08c1ebc42ad4bb0ad61acd1c63d68de52c6ae63c74e639390f85e91'),
    'vbi_dispatch': (0xEF4681, 64, '161234434ac6f861075f55a3b7b5e4643a45c7b72045b225e4c6a3cdbc78cd6d'),
    'vbi_register': (0x14CA580, 42, '896bc70922e9954e08c838c449f51dbcdc428842a4c25dd6a80bdea2c4cc6a26'),
}

SIMTICK_SITES = {
    'game_client_move': (0x122B360, 24, '8e123f79687ad689c48d2eaf879253c42d1359f129ddebd2c2d51ea6658a46d0'),
    'pathing_wait_swap': (0xEAC170, 24, '718b690e9f3d2779d2f91c812253fb26bfa1f4ce4cd73accae7229675e261f12'),
    'ui_controller_move': (0xB3BAD0, 28, '893307c89a256e16553b164d9d16ec4f0c3187060ebd86513b8f55694f1378e4'),
}

FIXEDTICK_SITES = {
    'main_loop_frame_call': (0x12AE9C7, 24, '119ee2003ab898668e3e485723f196ff83e43d99b3e8f26f1394c59b24541d26'),
    'frame_tick_prologue': (0xFA2D70, 32, '238662a6fc129b269aec1dbf9fff25fe1aa80138c8fc3be6f996dd65921f30e7'),
    'frame_tick_dt': (0xFA32D2, 58, 'a5bc59769a92153bec1c80192aa0d71459768e85442c9c5b8d2c72536701bfd7'),
    # XPhys::Scene::Step: Jolt PhysicsSystem::Update(dt, *(*0x6cf1510) collision steps, ...).
    'physics_collision_steps': (0xBA8CE, 39, 'c667e74f191023745d6e737e530ba1753a9dcc02c8b5dc9e70247d8298f3ce8e'),
}

# Component-walk profiling (overhaul): primary-vtable Move slot 674 per class.
# (function RVA, vtable entry RVA, SHA-256 of the function's first 32 bytes). The entry is
# relocated, so it is compared at runtime against image base + function RVA.
WALK_SITES = {
    'galaxy': (0x748120, 0x2B17348, '7c0981e988f37f237e70006b1453ce0e444fad8aba53e984c09ecba96e188a86'),
    'cluster': (0x3B8280, 0x2AA39A8, '51cd721d73d5812394c24f2feaf557276a8cedc867559ea49c7ff15026869e91'),
    'sector': (0x784230, 0x2B1FF58, '25b24d9be58c6e66ccb588207d0e3b13a8135d4a1d2eb100fd567c5e4cd24858'),
    'zone': (0x834950, 0x2B67590, '32b67bb8c930181f5eec4953b151d556af8d792652e0c50a967d4690dd75c9bf'),
    'ship': (0x7CC810, 0x2B85FA8, '809f44f2b8b4712e0ca1a54404595f609dd2c134847e4c884363061a7277649c'),
    'station': (0x41A430, 0x2B779E8, 'cdf874aac2d43fa934141f7bf472c51a9ac539a6f97b4f8bb57bebcb5c47e986'),
}
# Further vtables whose Move entry holds the same function as a primary class above
# (class, vtable entry RVA). S/M/L/XL ships each have their own vtable; the base 'ship'
# vtable alone is never reached by the walk.
WALK_EXTRA_ENTRIES = [
    ('ship', 0x2B813C8),   # ship_s
    ('ship', 0x2B83950),   # ship_m
    ('ship', 0x2B9D508),   # ship_l
    ('ship', 0x2B9AF98),   # ship_xl
]
WALK_CODE_SITES = {f'walk_{name}_move': (fn, 32, sha) for name, (fn, _entry, sha) in WALK_SITES.items()}

# Component-walk parallelism (rw-walk): stage 0/1 deferred-event hooks. Entry detours on the event
# functions and the galaxy walk call site; byte-pinned so the native layer's in-process patch fails
# closed on any build mismatch. First 32 bytes of each function (no base relocations in range); the
# galaxy call site is the 6-byte `call [rax+0x1510]` at 0x9fdf95 (vtable slot 674 = the whole walk).
WALKPAR_SITES = {
    'post_event': (0x99A3F0, 32, 'dd8eca5af59dbc3cdbb685d0a42e5d4324524a1f100fdb9a8e22088eec499df6'),
    'cancel_pending_events': (0x99A870, 32, '5fa21d29d3814d0caee1959d28b06943cbd80af1ea71d7b3daeedebd44b4c3d3'),
    'deliver_event': (0x99C470, 32, '68495212139fb6021fef265da1417ea5b63fff7273223d4a66fd7d7c7531c8fd'),
    'flush_command_queue_wp': (0x99ABE0, 32, '620991e9852f6df27038c7b746fd02825e2e27b177522e608247405445ec1df2'),
    'galaxy_walk_call': (0x9FDF95, 6, '06fb8e64fd4cfe4e113548c517924cf04284f71137cbf513b861888669b074c5'),
    'walk_registry_find': (0xCED70, 32, 'de126e38d2532250fc386f851f355cdb4812bf6b4507ace0549728e27505f268'),
    'walk_dynamic_cast': (0x19EFB18, 32, 'fd90f2d45c0b2f5e2507ca0c07198bc3572ea03948f1302f226b7048ea3e592f'),
    'walk_get_listeners': (0x999AE0, 32, 'fb18c04c970d8402e8ef0ba5588360b145b99cadfd9210eb324671b6690cf2b4'),
}
WALKPAR_GALAXY_CALL_RVA = 0x9FDF95

SITES = {**COMMON_SITES, **SIMTICK_SITES, **FIXEDTICK_SITES, **WALK_CODE_SITES, **WALKPAR_SITES}


def c_header() -> str:
    lines = [
        '#pragma once',
        f'#define X4_FILE_SHA256 "{EXECUTABLE["sha256"]}"',
        f'#define X4_FILE_SIZE {EXECUTABLE["size"]}',
        f'#define X4_PE_TIMESTAMP 0x{EXECUTABLE["timestamp"]:08x}u',
        f'#define X4_IMAGE_SIZE 0x{EXECUTABLE["image_size"]:x}u',
        'typedef struct { size_t rva; ULONG length; const char *sha; } x4_signature_check;',
    ]
    for name, sites in [('common', COMMON_SITES), ('simtick', SIMTICK_SITES), ('fixedtick', FIXEDTICK_SITES)]:
        lines.append(f'static const x4_signature_check x4_{name}_checks[] = {{')
        for rva, length, sha in sites.values():
            lines.append(f'    {{0x{rva:x}, {length}, "{sha}"}},')
        lines.append('};')
    lines.append('typedef struct { const char *name; size_t fn; size_t entry; ULONG length; const char *sha; } x4_walk_site;')
    lines.append('static const x4_walk_site x4_walk_sites[] = {')
    for name, (fn, entry, sha) in WALK_SITES.items():
        lines.append(f'    {{"{name}", 0x{fn:x}, 0x{entry:x}, 32, "{sha}"}},')
    lines.append('};')
    classes = list(WALK_SITES)
    lines.append('typedef struct { int cls; size_t entry; } x4_walk_extra;')
    lines.append('static const x4_walk_extra x4_walk_extras[] = {')
    for name, entry in WALK_EXTRA_ENTRIES:
        lines.append(f'    {{{classes.index(name)}, 0x{entry:x}}},')
    lines.append('};')
    # Component-walk parallelism detour targets (rw-walk). walkpar.h verifies these before the native
    # layer installs its entry detours; any miss fails closed.
    lines.append('static const x4_signature_check x4_walkpar_checks[] = {')
    for rva, length, sha in WALKPAR_SITES.values():
        lines.append(f'    {{0x{rva:x}, {length}, "{sha}"}},')
    lines.append('};')
    lines.append('#define X4_HAVE_WALKPAR_SITES 1')
    lines.append(f'#define WP_GALAXY_CALL_RVA 0x{WALKPAR_GALAXY_CALL_RVA:x}u')
    return '\n'.join(lines) + '\n'
