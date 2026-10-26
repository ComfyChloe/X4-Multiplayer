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

# Physics/flight-job probe (physics agent, physics_probe.h). Wrapped .rdata vtable entries
# (function RVA, vtable entry RVA): XPhys::XJobSystem (vtable 0x2A2ABA0) slots 2 CreateJob and
# 5 WaitForJobs, XPhys::XContactListener (vtable 0x2A2AB48) slots 2-4 (added/persisted/removed).
# Order matters: physics_probe.h indexes PP_CREATE, PP_WAIT, PP_ADDED, PP_PERSISTED, PP_REMOVED.
PHYSICS_VTABLE_SITES = {
    'job_create': (0xC5960, 0x2A2ABB0),
    'job_wait': (0xC5A70, 0x2A2ABC8),
    'contact_added': (0xB7230, 0x2A2AB58),
    'contact_persisted': (0xB7250, 0x2A2AB60),
    'contact_removed': (0xB7270, 0x2A2AB68),
}
# Mode 'joltwait' swaps XJobSystem slots 3 CreateBarrier and 4 DestroyBarrier for the serial barrier of
# physics_jobs.h (order matters: PP_MODE_CREATE_BARRIER, PP_MODE_DESTROY_BARRIER).
PHYSICS_MODE_VTABLE_SITES = {
    'job_create_barrier': (0xC5A30, 0x2A2ABB8),
    'job_destroy_barrier': (0xC5A60, 0x2A2ABC0),
}
# Code whose bytes establish what the probe reads or calls (no base relocations in any range).
PHYSICS_SITES = {
    'jolt_job_max_concurrency': (0xC5940, 24, 'ea41983c8853e368253719bebeee39612ea260cb5719e633669388722b04aaa6'),
    'jolt_job_create': (0xC5960, 32, '90b58768bed65170d856bb9eadac6760434f70cde60c08a1ac629c29e0b1706f'),
    'jolt_job_wait_thunk': (0xC5A70, 8, '126fdbc39df631064fcd8df5d39460f2db0ad228bbf7dbe6bfd1ee8694ac4fb3'),
    'jolt_job_queue': (0xC5A80, 75, 'e3fa7f7b2c8d552814fff09dff30cc808283bd8fca8720c9ca913c8ced79e291'),
    'jolt_job_wait': (0xC5DC0, 32, '0b62755d3cdd0a951e1d59030d2c92987b7405d6248f9c2034f542fa7e1e2d15'),
    'jolt_contact_added': (0xB7230, 29, '7a528abe5d453d86f4755a82afec551e75b7fab3e4791e870cb00ed486610d75'),
    'jolt_contact_persisted': (0xB7250, 29, '7058941a117d0b6ef7e0cb35bca789d317f1759696a2af94577cd068cb7db720'),
    'jolt_contact_removed': (0xB7270, 32, '7277e9ef726c6babb41bfbf578c7db1ca4a30f89d057b199badc078f69a7dace'),
    'scene_step_sync_list': (0xBA6C4, 42, '0a8c6e1d5c6e770a7edae6172ee82e844f3371076166defc0ca9b84f8d0b1e0a'),
    'scene_step_update': (0xBA8BC, 57, '52de8a3cbc7b41b1d6d2369ad1cff3acc09cd221e8d193b12b05879e1dc63dfe'),
    'universe_scene_steps': (0x9FE42C, 100, '263d32f7db18493a422c0d1ffec8a2dd3768e285b803ef74346d638ccb79b83e'),
    'pathing_update_batch': (0xEAED43, 198, 'c047f7b9fc40c34a0d9b1fdbac1c9963907035a93965441b4e2b048e7686f3b0'),
    'pathing_run_pass_batches': (0xEAE8EF, 182, '5e5821cfe341edc82a3c5d5d4d4d11417b1d2fde28062a80dae3132da33ba8d3'),
    'jtm_thread_job_begin': (0x14BC20D, 34, '80da2309c490a921bcddd4b550f1afda008908f2b8fbb0cdbfc8b73d62e40176'),
    'jtm_thread_job_end': (0x14BC27C, 40, '38f5afff39b6887473f0be6e8fd5a923c2b879a876420d9740e17694bc45b1f5'),
    'jtm_thread_wait': (0x14BC0C0, 64, '2047329aa41dd9a836818c09d3bb0a498a2c8cf3c0041e50ec8d3d2c5f6d9095'),
    'jtm_thread_count': (0x14BD095, 75, '1cdaade74bb0a78b3e6c4a556565cc531071d26ec2c627ad7c4fc24cf3286599'),
    'jolt_job_execute': (0xB4100, 111, 'c7c3156704013bcbb39109714376978ceb299fbc1edf753582cf55f214604159'),
    'jolt_job_create_barrier': (0xC5A30, 45, '45dbdeb74471efc89a16d59350ff425eab8709689e7871f604590accac6eadf2'),
    'jolt_job_destroy_barrier': (0xC5A60, 5, 'cfe8e60203df08650126edcd8f93b5e2efa56fc4bf2bb69690910ae46ac126b4'),
    'jolt_job_free': (0xC5B30, 48, '36a288ff020ae17fca8379237d8cea6f9f94acd484f445ca3c44938e29e66cff'),
    'jolt_barrier_on_finished': (0xC5F90, 48, '7090eda16704c9dbd775e4782a1ae134cef80db0517b159402d6e71d24c6da0f'),
}

SITES = {**COMMON_SITES, **SIMTICK_SITES, **FIXEDTICK_SITES, **WALK_CODE_SITES, **PHYSICS_SITES}


def c_header() -> str:
    lines = [
        '#pragma once',
        f'#define X4_FILE_SHA256 "{EXECUTABLE["sha256"]}"',
        f'#define X4_FILE_SIZE {EXECUTABLE["size"]}',
        f'#define X4_PE_TIMESTAMP 0x{EXECUTABLE["timestamp"]:08x}u',
        f'#define X4_IMAGE_SIZE 0x{EXECUTABLE["image_size"]:x}u',
        'typedef struct { size_t rva; ULONG length; const char *sha; } x4_signature_check;',
    ]
    for name, sites in [('common', COMMON_SITES), ('simtick', SIMTICK_SITES), ('fixedtick', FIXEDTICK_SITES),
                        ('physics', PHYSICS_SITES)]:
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
    lines.append('typedef struct { const char *name; size_t fn; size_t entry; } x4_physics_vtable;')
    for table, sites in (('x4_physics_vtables', PHYSICS_VTABLE_SITES), ('x4_physics_mode_vtables', PHYSICS_MODE_VTABLE_SITES)):
        lines.append(f'static const x4_physics_vtable {table}[] = {{')
        for name, (fn, entry) in sites.items():
            lines.append(f'    {{"{name}", 0x{fn:x}, 0x{entry:x}}},')
        lines.append('};')
    return '\n'.join(lines) + '\n'
