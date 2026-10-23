SOURCE-INSPECTION PUBLICATION PREPARATION — 23 OCTOBER 2026

DECISION / STATUS
The owner requested preparation for public read/download access and explicitly
selected restrictions on further reuse and modified redistribution. Preparation
is complete; no repository has been made public and no public repo was created.
The proposed destination is a NEW repository, Lith7ium/X4-Multiplayer-Source.
Keep Lith7ium/X4-Multiplayer private. Do not change its visibility or rewrite it.

Reviewable candidate:
  Desktop/X4 Online/Source Publication Review/7.3.0-2026-10-23/
  X4-Multiplayer-Source/       independent Git repository, only main / one commit
  X4-Multiplayer-7.3.0-Source-Inspection.zip  public-source candidate, 410,371 bytes
  REVIEW.json                private review scope, results and publication gates
  SHA256SUMS.txt              source archive checksum

Snapshot commit: b4cb16c98a417e2182ce5e5e6a09a8016a9227f6
Source revision: f29d3efa7dfae2bf3c9ff113197572a3973c6085 (internal 7.3.0)
Source ZIP SHA256: c9a3173005d26206e99314aa625bafa4a93fc3578a306496387b94dddc75bdbc
The snapshot has no origin and no link to the private repository's object store.
Only its reviewed source tree/commit and ZIP are publication candidates, not this
private preparation folder or the entire Desktop review folder.

WHY A SEPARATE REPOSITORY
A read-only authenticated mirror audit covered 280 commits, 73 advertised refs
(43 PR refs), 1,044 blobs / 8,037,300 bytes. Pattern checks found personal-path,
Slack-reference and test-network material in history. Commit metadata also
contains personal contributor emails. No provider credential, private-key,
credential-literal or embedded-URL-auth pattern matched. This is bounded pattern
coverage, not a guarantee that no secrets exist anywhere.

GitHub currently has zero Actions runs and zero releases, plus 4 ordinary issues
and 28 PRs. Discussion bodies/attachments and unreachable server objects were not
certified for publication. They are not part of the independent source snapshot.
Deleting today's internal reports would not remove old commits or GitHub records.
Keep them private rather than destroying shared development/review history.

EXPORTED CONTENT AND VERIFICATION
189 files: 181 original source/test files, seven new public documents/ignore file
and a generated manifest. 176 original files are byte-identical. Five tests
replace internal network/profile examples with synthetic values; every substitution
is recorded by source/published SHA256 in SOURCE-MANIFEST.json. Production source
is unchanged. The prepared Git commit uses the owner's public GitHub noreply
identity and contains no private contributor history.

The allowlist excludes internal reports, agent instructions/handoffs, old GitHub
discussions, executables, compiled libraries, game assets, schemas, saves, runtimes
and session data. It does not fetch any third-party source or runtime. Historical
packaging source/templates are labeled for inspection, not a playable release.

Checks: 8 export-boundary regressions PASS; 42 affected Python/LuaJIT checks PASS
without skips; applicable PowerShell fixtures PASS on Linux, with 2 Windows-only
drive-root assertions NOT RUN and native metadata simulated. Tests used a separate
disposable copy with private build prerequisites; those were never added to the
candidate. Source hashes, 28 public-document links, Python syntax, ZIP CRC and
exact archive inventory pass. No X4/gameplay/full-suite rerun was needed.
Machine-readable scope: review-20261023.json. Raw local audit/test evidence:
.local/publication-review/. Original packages and installation were not changed.

LICENSE AND OWNER REVIEW
templates/LICENSE.txt is the proposed custom Source Inspection License, not an
open-source license. It permits viewing and retaining unchanged private copies
for inspection. It grants no additional permission to modify, compile/run,
redistribute, sublicense or commercially reuse the source. GitHub's applicable
hosting/viewing/forking rights and mandatory legal rights take precedence. The
license cannot technically prevent copying or override platform terms. Separate
internal tester/contributor permissions are preserved.

The owner must approve the actual wording and have authority from relevant
contributors to publish/license their code. This preparation did not obtain new
third-party contributor consents or professional legal review. No claim is made
that GitHub grants can be narrowed by this custom license. Publication is not a
blanket ownership claim over game APIs, trademarks or third-party dependencies.

FINAL PUBLICATION STEP — ONLY AFTER EXPLICIT OWNER APPROVAL
1. Review README.md, LICENSE.txt, POLICY.md, STATUS.md and SOURCE-MANIFEST.json in
   the candidate. Verify the candidate is still at the exact snapshot commit,
   with a clean working tree, only main, one commit and no remotes/alternates.
2. Create a NEW public personal repository named X4-Multiplayer-Source from that
   candidate only. Do not fork, mirror-push, rename or publicize the private repo.
   With authenticated gh, from the candidate directory, the intended command is:
     gh repo create Lith7ium/X4-Multiplayer-Source --public --source=. --remote origin --push --disable-issues --disable-wiki
   This command has NOT been run. Stop if that repository already exists; inspect
   its ownership/history before considering any update. Never force-push it.
3. Do not invite read-only viewers as collaborators. They can view/download the
   public repository without write access. Keep publication writes owner-only.
   No workflow, private release assets or internal issue/PR history is supplied.
4. Verify anonymous browsing/download, license, branch/tree and write permissions
   after publication. Report the actual public URL, not a guessed pre-created URL.

FUTURE UPDATES
Run prepare.py with an explicitly reviewed source revision and a NEW destination:
  python3 publication/prepare.py --revision <commit> --destination .local/<new-candidate>
Inspect excluded files, privacy findings, source diffs and ownership/license scope.
The exporter refuses existing destinations, unknown source file types, symlinks
and matched private/credential content. Only the reviewed test fixtures can be
substituted. A scan failure requires investigation, not broad suppression.

After approval, update only the public repository's files in its own existing
history; do not merge private branches. Preserve previous public snapshot commits.
Recheck affected behavior only. Publishing source does not authorize public mod
packages, a new playtest, or changes to live installations.

PRIMARY REFERENCES CONSULTED
https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository
https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#d-user-generated-content
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
