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

# AI phase census / per-sector AI driver (ai-driver, native/engine/aidriver.h). Code ranges hashed before
# any patch: the whole AIDirector::Move body pins every inner call site and its frame layout.
AIDRIVER_CODE_SITES = {
    'ai_frame_calls': (0x9FE080, 32, 'e5a8dc86de93977def723836b9479f24291ecb4a35117dfef142cf9f567b82ea'),
    'ai_move_body': (0x106D250, 0x140E, '360cdc20473b7acde457f95c71540da76295a706b8e7509d9b8466a19e706018'),
    'ai_exec_run': (0xB58BB0, 0x12F, '7509510d2582f326fb87c58a23b715d1f7499577bf681c67377347314e3aa759'),
    'ai_irq_run': (0xB58CE0, 32, '8486f9ae019f1ddfdc0abe9f405f3948e647368dfe1f321e395ac9bbb7978faf'),
    'ai_request_wake': (0x106F910, 0x67, 'da5cdd3e66f63039820bd06f8bca198ad6bc7f02433361157f54f695af893c91'),
    'ai_clear_wake': (0x10669C0, 0xB5, 'ee0472216b11cfa19adbabf701c6147d6841e62850c6704ba0274b1e890c0a8f'),
    'ai_ctx_event': (0x1066750, 32, '28be89dd71415b9965f178729efab95fff2e586b2c435ab9c774e90be30ca1f4'),
    'ai_stack_event': (0x1062F70, 32, 'b57a76b0c72966487a4d26431fef60d77c49378f0bf8dd59283213c01f8b310e'),
    'ai_cue_event': (0x111F990, 32, '16141305ae7036c9128223e1d14b15b5e81e58ee801672e7dfb09cf3ffb9ec17'),
}
# E8 rel32 call sites redirected through a near stub: (site RVA, original callee RVA). 'ai_phase' is the
# AIDirector::Move call in UniverseFrameUpdate; the others sit inside Move (census only).
AIDRIVER_CALL_SITES = {
    'ai_phase': (0x9FE093, 0x106D250),
    'exec_run': (0x106E3F6, 0xB58BB0),
    'irq_global': (0x106D8D6, 0xB58CE0),
    'irq_interrupt': (0x106E2EF, 0xB58CE0),
    'irq_attention': (0x106E34A, 0xB58CE0),
}
# Data-only: vtable slot 565 (HandleScriptEvent) entries (entry RVA, function RVA).
AIDRIVER_VTABLE_SITES = {
    'ctx_event': (0x2C6B418, 0x1066750),    # AI::AIScriptContext
    'stack_event': (0x2C6A260, 0x1062F70),  # AI::AICallStackContext
    'cue_event': (0x2C76318, 0x111F990),    # MD::Cue
}
# Stage-1 AI driver (aidriver_move.h): every engine helper our AIDirector::Move reimplementation calls,
# first 32 bytes pinned (verified before a normal/rounds/compare mode is allowed).
AIDRIVER_HELPERS = {
    'ai_clear_timed_wake': (0x1066950, 32, '14fcca56115c1d9d18022337aa87361aa8ae092525a538137207d38fa38b9af7'),
    'ai_clear_timed_wake_if_due': (0x1066A80, 32, '27cf57ab0f048c63fb45e19e847a2e31c746dd74057af3fe13092b3ac6d8974a'),
    'ai_remove_context': (0x106F710, 32, 'ecf9d03b31b13e057fa600ed545710c38d5dfcf23d56b0f3913b80ab1ac13236'),
    'ai_tree_extract': (0xB2CF0, 32, 'b3d5f7933fb22f2af6d73863d9c0e260fc265ab944b8681d443265faeb2bb330'),
    'ai_vec_insert': (0xB1880, 32, 'dcdb92051721c7becf4eed424e8ca0d42d2a1b3bec56f63b0a94cdad054ad7ce'),
    'ai_blocking_action': (0x109A6A0, 32, '1a21c245e92d1f7047641712e41be5e83c108ad811839ff7799f57a4a87a584f'),
    'ai_attention_changed': (0x10664A0, 32, 'c13c2768f2d3760e40bf65c5a0e96ca86af59c6ce5506119f883e1c713759248'),
    'ai_attention_block': (0x1074850, 32, 'e988c208d8919def4998f7eb4ad3f51d05b34c65839d953584fcebbc5582067f'),
    'ai_jump_to_label': (0xB59F30, 32, '67fffa66aa853b72a7fe3b626d3dafcbe75158165e74d883dd7f6f3edb772226'),
    'ai_unregister_all': (0x998B20, 32, '0b0051070a3092ecc4255cf416f85f192968c3ec8bba3037ee3ccb663f924064'),
    'ai_ctx_reset': (0xCBD2C0, 32, '69080708332ed0ac141c7658b122ea142a8a4034fba994576c1f7610827c2a38'),
    'ai_record_clear': (0xC58820, 32, '76617864491cdf83cc0b52a0fd56daeca32f6c17dfb8a2f44ceb9b9febbf9f8d'),
    'ai_record_copy': (0x106E660, 32, 'a017cca849e651013326841adb133a3c93cb9f6b1f7694e4864d04d398351ad6'),
    'ai_value_release': (0xCCCAA0, 32, '8f17ca2eeb752229787896fee4ee91537ef9fd49e3dbd0f959772b7aaf722ed8'),
    'ai_value_type_error': (0xCCCDA0, 32, '8ee7e3090e63c02c37bc05abe9b22493b6dfd8b4f06a32a0a0ec6f0ed0456a1e'),
    'ai_value_assign': (0xCCD540, 32, 'e5116236fa80b2bc16c9f5c8610e271be25be76ea686f3dd464cee423a71b654'),
    'ai_resume_after_restore': (0x10666E0, 32, '01a9444787c61c8cd279ad055bac38e71a3cc503adc5a246c2f9df545e761ae9'),
    'ai_return_run_script': (0x10A5A90, 32, 'fda46a609f1a9b9e57fbaaccd31f037d0bc1a14f92e1e4239f1703c6d086b18c'),
    'ai_return_order_value': (0xCC6710, 32, '7c4ece18f3f11d5da88805175cf530e9d8f0b49bda3f2bf76660e1f5a1260964'),
    'ai_frame_context': (0xCC1A00, 32, '65a2347f45def1cd873430cbd32a94117e74c80ecaa2be216073fb1de1679f00'),
    'ai_value_error': (0xCC9A70, 32, '50506138c7d4719b2ab8cf68ef26188f2aabcb89c2f3f00fd46752f343b6aedc'),
    'ai_eval_string': (0xCC7F10, 32, '5b77d9b7f4b48bde023390eacb431d4910afdaebda8de1de075f2df1ee9a0735'),
    'ai_find_script': (0x106D0A0, 32, '10983813a505f8fc3eb01ca0939230728b91b1ca6880f3a9e9734d0eb0b016ba'),
    'ai_eval_bool': (0xCC7DA0, 32, '863cf8eddad86bb2a81a96f74f136b2ddbff2b80c230de4306ce8370555d7e4f'),
    'ai_expr_error': (0xCC8E20, 32, '67da488cfd4852e0058baaac867208d880639bb6f1ad7c538d63fdbb3edad840'),
    'ai_string_assign': (0x975A0, 32, '97e39e1889777bac6ef11cc83af8d816949e71eae015ec0c21defa9408f6c288'),
    'ai_abort_reason': (0xACB4E0, 32, '49534230aa6da160a2d56f4c7ca7323488e2f7f3437911012bb6af085cba2130'),
    'ai_abort_stack': (0x106FD90, 32, 'fdb991f7d801bd3d5af70dd00d3bf0faaf1a9c8a579d8790f7afbb8144c6e3a9'),
    'ai_push_script': (0x106ECA0, 32, '71b87312c12069f614a9a103f3c9b25049a31d1178c0cdc40c8d65983bbac4b8'),
    'ai_tree_erase': (0xB4B810, 32, '5be9fc16898ec2caa71e8c079808ad178a96d3412e4e26bc1543481d788dedc8'),
    'ai_refresh_scripts': (0x1068140, 32, '654445064970db42d1c4f6e61060e02a79b3ce1b4c76c5097e733381201b25b2'),
    'ai_list_clear': (0x10C4220, 32, '0408f9a72e493efbd7690faf54b10f98bd7c7617c6783ef6519217b23d145d62'),
    'ai_str_format': (0x14B7A50, 32, '20bcc86710371a9ffa8997b393f29d68218aa823ee6262876a2d74eed3476434'),
    'ai_log_error': (0x14B9A30, 32, '513a00e5cfe1cff30162ec5d312038a402c3a0713318ebc3f92293179b665a66'),
    'ai_id_to_string': (0x1303B0, 32, 'dd0b51cf2ecb06dff927891d1cd743d99fba18a23bbd86997b97e7c9fe0a3d0d'),
    'ai_op_delete': (0x174F310, 32, 'ad01bfb03a6b4e0c93655804766a34704848314f0016934327f7a84e1db3d672'),
    'ai_invalid_parameter': (0x19FC9C0, 32, 'dbd9cfef1563f27b45b389cfe4a4f8d77e2dab2f97ea9a0505db88029d256d7e'),
    'ai_tls_init': (0x174F5FC, 32, '1995a81f504d7d52e7d6b8e5ac3fa31dc5b6dd83b5796b45f29a73a41e445d1d'),
}

SITES = {**COMMON_SITES, **SIMTICK_SITES, **FIXEDTICK_SITES, **WALK_CODE_SITES, **AIDRIVER_CODE_SITES,
         **AIDRIVER_HELPERS}


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
    lines.append('static const x4_signature_check x4_aidriver_checks[] = {')
    for rva, length, sha in AIDRIVER_CODE_SITES.values():
        lines.append(f'    {{0x{rva:x}, {length}, "{sha}"}},')
    lines.append('};')
    lines.append('static const x4_signature_check x4_aidriver_helper_checks[] = {')
    for rva, length, sha in AIDRIVER_HELPERS.values():
        lines.append(f'    {{0x{rva:x}, {length}, "{sha}"}},')
    lines.append('};')
    lines.append('typedef struct { const char *name; size_t site; size_t target; } x4_ai_call;')
    lines.append('static const x4_ai_call x4_ai_calls[] = {')
    for name, (site, target) in AIDRIVER_CALL_SITES.items():
        lines.append(f'    {{"{name}", 0x{site:x}, 0x{target:x}}},')
    lines.append('};')
    lines.append('typedef struct { const char *name; size_t entry; size_t fn; } x4_ai_vsite;')
    lines.append('static const x4_ai_vsite x4_ai_vsites[] = {')
    for name, (entry, fn) in AIDRIVER_VTABLE_SITES.items():
        lines.append(f'    {{"{name}", 0x{entry:x}, 0x{fn:x}}},')
    lines.append('};')
    return '\n'.join(lines) + '\n'
