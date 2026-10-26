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

# Stage 3 (fixed60stage3): the main loop's 16 ms WaitForSingleObject(g_semSimDone) call
# (mov edx / mov rcx / call [rip] / ... / cmp eax, WAIT_TIMEOUT), FrameTick's threaded
# present tail that fixes the re-present sequence and its arguments, and the entries of
# the three functions the wrapper calls. All RIP-relative; no base relocations in range.
STAGE3_SITES = {
    'main_loop_wait_call': (0x12AE889, 36, '473d2a6ad6b095ddf93ec5ac62883b8eb284e10d4349485555cfd51b5c9e4ccf'),
    'frame_tick_present_tail': (0xFA343C, 50, '94ea2b527afbe4ef29abdb488102328da4013b371b1cb4001e6d28cfd6a9a032'),
    'begin_threaded_render_frame': (0xFA23A0, 47, '3fbe4c720404ea5780ab03d1cc72dd496890800f64609cd291833d6f4abf70b6'),
    'render_previous_frame': (0xF9FF40, 47, 'c9e1682c8c8fd329fe369ed1aaf6f217dad3be115c24525c51af3057bbc5a582'),
    'present_and_frame_limit': (0xFA2BA0, 23, 'acbe4b2bf1b13ad0fc66fdab087a247a18d7778e304b7b9ec306c6f3af0a07c2'),
}

SITES = {**COMMON_SITES, **SIMTICK_SITES, **FIXEDTICK_SITES, **STAGE3_SITES}


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
                        ('stage3', STAGE3_SITES)]:
        lines.append(f'static const x4_signature_check x4_{name}_checks[] = {{')
        for rva, length, sha in sites.values():
            lines.append(f'    {{0x{rva:x}, {length}, "{sha}"}},')
        lines.append('};')
    return '\n'.join(lines) + '\n'
