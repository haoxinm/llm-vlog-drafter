# Resolve MCP research (2026-10-25)

## Access limits
- WebFetch cannot resolve any third-party host (ENOTFOUND: digitalproduction.com, ymcinema.com, motionmedia.com, grainzilla.com, explainx.ai). curl to www.blackmagicdesign.com is refused by the egress proxy (403 connect_rejected). No Blackmagic page, manual or forum thread was read. `gh api` for danielbaldwin47/resolve-mcp#263 was refused (repo not attached), but WebFetch of the github.com URL worked.
- Everything below about the BUILT-IN server is secondhand (search snippets plus one GitHub issue). Nothing is first-party.

## 1. Resolve 21.1 built-in MCP
Verified-ish (several consistent secondary sources):
- Shipped in 21.1 (released 2026-09-08 per digitalproduction.com/2026/09/08/resolve-21-1-adds-mcp-and-finally-gets-presets/ and ymcinema.com/2026/09/17/...; both seen only as search snippets). Not in 21.0.
- Enabled via File > Setup AI Assistants. Resolve detects Claude Desktop, Claude Code and Codex and writes their config (ymcinema snippet).
- Studio only, because 21.1 moved Python scripting to Studio (see samuelgursky README and its docs/reference/resolve_scripting_changelog_21.1.md).
- Transport is stdio. Evidence is one issue, https://github.com/danielbaldwin47/resolve-mcp/issues/263, which says the server is `C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolveMCP.exe` (stdio) and that it is also shipped as `DaVinciResolve.mcpb` for Claude Desktop. It attaches to Resolve through fusionscript to fuscript on port 1144. The issue also says the port and auth are not described.
- Tools: `--dump-tools` reportedly lists 14. Named: launch_resolve, get_resolve_status, get_whats_new, get_scripting_api, search_scripting_api, get_scripting_docs, run_script (sandboxed: blocks os/sys/pathlib/open, 10 s default, 60 s max, fresh process per call, `resolve` and `project` pre-injected), run_script_unsafe, plus LUT/DCTL helpers. Issue wording: "a self-documenting Python escape hatch, not a tool catalog". So it is NOT a typed set of project/import/relink/marker/title tools; the agent writes Python against the scripting API. Press claims of "100+ tools" are unverified, and the issue says the "88 tools" figure belongs to a third-party project.
- The issue also reports no reconnect: it wedged after a mid-session Resolve restart.
Unverified: port or preference for HTTP (none found), auth model, macOS vs Windows (issue is Windows; the .mcpb suggests both), the 14-tool list in full, whether "External scripting using = Local" is required (secondary guides say yes). The manual section on AI assistants (reported "p. 4318") was not read.

## 2. samuelgursky/davinci-resolve-mcp (cloned, read only, nothing executed)
- Cloned locally for reading only (not executed)
- License MIT. Version 4.15.3, HEAD commit 2026-10-23 "chore(release): bump version to 4.15.3". Shallow clone, so history depth is unknown. It is very active: CHANGELOG, 21.1 alignment docs, issue numbers above #260.
- Python 3.10+ (FastMCP), plus an optional Node "advanced" server that edits .drp/.drt files offline. Default transport stdio; `--transport sse|streamable-http` opt-in, loopback with bearer token (SECURITY.md).
- Connection: official DaVinciResolveScript. Studio needs Preferences > General > External scripting = Local. The free edition is not supported for external scripting; it has an "in-app bridge" (Workspace > Scripts) that the README says is a 21.0.x-only path because 21.1 removed .py from the Scripts menu on free.
- OS: macOS, Windows, Linux; Resolve 18.5+.
- Surface: compound mode has 37 tools with an `action` parameter; `--full` has 390 granular tools. Tools: setup, resolve_control, project_manager, project_settings, render, media_storage, media_pool, folder, media_pool_item(_markers), timeline, timeline_markers, timeline_item(_fusion/_color), timeline_ai, edit_engine, timeline_versioning, gallery, graph, color_group, fusion_comp, fuse_plugin, lut, dctl, script_plugin, layout_presets, knowledge, others.
- Actions I need (all in src/server.py):
  - project_manager: create, load, save, close, delete, safe_project_create, safe_set_project_settings, project_settings_snapshot.
  - project_settings: get_setting, set_setting.
  - media_pool: import_media, safe_import_media, import_timeline (ImportTimelineFromFile; whitelisted options timelineName, importSourceClips, sourceClipsPath, sourceClipsFolders, interlaceProcessing, line 11843), append_to_timeline (clip_infos with startFrame, endFrame, trackIndex, recordFrame), create_timeline_from_clips, relink and safe_relink (RelinkClips(clips, folder_path)), delete_timelines (confirm token), replace_clip.
  - timeline: import_timeline_checked (renames the XML sequence, errors if Resolve returns an existing timeline), detect_missing_media, build_relink_plan, probe_interchange_roundtrip, add_track, insert_title, insert_fusion_title, insert_generator, set_title_text, get_title_text, bulk_set_title_text, title_property_scan, export.
  - timeline_markers: add, get_all, delete_*.
  - timeline_item: get_name, get_start, get_end, get_source_start_frame, get_source_end_frame, get_media_pool_item, get_property, add_transition (21.1).
  - render: add_job, start, get_job_status, verify_output, plus presets.
- Safety:
  - README and server.py line 442 say it "does not execute caller-supplied code (script_plugin execution was removed in v3.0.0)".
  - It has confirm_token gating for destructive actions (delete_timelines etc.), an AI-governance gate and an AI ledger.
  - Destructive tools still exist: project_manager delete, delete_timelines, delete_clips, delete_all_jobs, safe_project_delete.
  - script_plugin install/remove writes into Resolve's script folders. Install.py edits the user's MCP client configs.
  - It checks GitHub for updates, and it advertises a commercial add-on (Bradford Post Assistant). Pin the version.
- Own measurements (Studio 19.1.3.7, docs/guides/headless-edit-loop.md):
  - FCP7 XML, AAF and FCPXML 1.10 relink moved media via sourceClipsPath (3/3).
  - **DRT, EDL and OTIO fail to relink.** OTIO import "returned None" when the media had moved. With importSourceClips=False, OTIO arrives fully offline; OTIO links only with importSourceClips=True plus sourceClipsPath.
  - Round trip on a rich cut: FCP7 XML loses markers and clip colour. OTIO keeps markers but loses source in-points on that test. AAF resets transforms.
  - Reusing a timelineName makes import return None silently, and Resolve uses the name inside the file.
- Titles (docs/reference/api-limitations.md): InsertTitleIntoTimeline has no track index and lands on the Source selector target (V1). Workaround: put the title in its own timeline, then AppendToTimeline that timeline with trackIndex and recordFrame. Text edits afterwards via the Fusion comp `Template` StyledText.
- Subtitle text cannot be set by the API.

## 3. OTIO "5 media not found" root cause
- spike/out/interchange/phase0.otio has 5 ExternalReference clips (landscape_5s.mp4 twice, still_1080p.png, portrait_4s.mp4, music_6s.wav). Their target_url values are RELATIVE: `../../../tests/fixtures/generated/landscape_5s.mp4`, etc. The title is a MissingReference. So 5 ExternalReferences = "5 media files not found", an exact match.
- The same media is absolute in phase0.xml: `file://localhost/home/user/llm-vlog-drafter/tests/...`, and that imported cleanly. This matches the spike doc §6 and the samuelgursky evidence above.
- Why: FilmCraft writes relative paths when media and export share a top-level dir. Resolve's OTIO importer evidently does not resolve them against the .otio location (inferred from the symptom; I found no Blackmagic doc confirming it). The OTIO file-bundle docs (opentimelineio.readthedocs.io, via search snippet) say adapters expect `file:///abs/path` or a path relative to the current working directory, which is not the .otio folder.
- Not verified: Resolve's exact accepted target_url forms on Windows (`file:///C:/...` vs `C:\...`).
- Fix, cheapest first:
  1. Post-process the OTIO to absolute `file://` URLs (percent-encoded, `file:///C:/...` on Windows) and re-validate by reload. Our tools/resolve_verify.py already normalises these forms.
  2. Do not make OTIO primary. It needs importSourceClips=True and sourceClipsPath to link, and import fails outright if the media has moved.
  3. FCP7 XML stays primary: it already links, and its pathurl is absolute.
- AAF "cannot be opened" and the FCPXML crash / EDL freeze are separate. I have no evidence for the cause. FCPXML 1.10 exports as a bundle dir with Info.fcpxml, which is a possible factor for FCPXML only.

## 4. Strategies
(a) File interchange only (FCP7 XML) + tools/resolve_verify.py.
- Works today: the user's XML import was clean. Windows and macOS. Studio is needed only for the verifier, which already falls back to a manual checklist (exit 3).
- Does not fix: titles (the title becomes a clipitem with no media; the Source Text filter is not turned into Text+), markers (FCP7 XML loses markers per the samuelgursky measurements; the spike says the marker is in the XML, but it did not survive their import), clip colours.
- Cheapest and safest. CI-testable already.

(b) Deterministic Resolve adapter that is an MCP client.
- Fits DESIGN: allowlisted calls, FilmCraft stays the source of truth, read-back verification, fake MCP server in CI.
- Steps via samuelgursky: project_manager.create -> project_settings.set_setting (frame rate, resolution) -> media_pool.import_media -> media_pool.import_timeline(FCP7 XML, importSourceClips=False/True, sourceClipsPath, unique timelineName) -> timeline.detect_missing_media / media_pool.safe_relink -> timeline_markers.add -> title cards via nested-timeline route (timeline create, timeline.insert_fusion_title, timeline.set_title_text, media_pool.append_to_timeline with trackIndex/recordFrame) -> read back with timeline.get_items / timeline_item.* and compare to FilmCraft.
- Risks: the server is a 390-tool surface we would allowlist to about 15 actions; version drift (4.x weekly); Studio only; the title workaround is the least proven part; transitions via AddTransition are confirmed only by one contributor on 21.1.
- Does not need the built-in server: calling DaVinciResolveScript directly (as resolve_verify.py does) gives the same operations with no third-party dependency. An MCP layer is only worth it if we want a standard seam for tests.
- With the built-in server, (b) is not possible as a typed call set. We would have to send Python source via run_script, which is raw code, against DESIGN. The only exception would be sending a fixed, reviewed script template with parameters as JSON. That is technically allowlisted but is still run_script_unsafe-adjacent. Not recommended.

(c) LLM drives Resolve MCP directly.
- Technically possible, since both servers target exactly this. Violates the DESIGN principles (no LLM-generated commands, deterministic, read-back). The built-in server is an arbitrary-Python tool. Not testable in CI, not reproducible, and can delete projects. Reject for handoff.

## Recommendation
Do (a) now, with the OTIO exporter fixed or demoted. Build (b) behind an optional "resolve-finish" adapter, as a thin client of our own DaVinciResolveScript wrapper first, or of samuelgursky's server pinned to a version, using only the allowlisted actions above. Never (c).

## Open questions for the user
1. Studio or free edition? Everything above except file import needs Studio on 21.1.
2. Did the "Setup AI Assistants" menu produce a config you can show (file path and command)? That would settle transport, tool list and the 14 tools. Please run `ResolveMCP.exe --dump-tools`.
3. Can you send the Resolve Console output for the AAF, FCPXML and EDL failures, and re-run the OTIO once with absolute file:// URLs?
4. Do you accept Studio as a requirement for the optional adapter?
5. Is marker and title loss in the XML acceptable for v1, or must titles arrive as Text+?

## Addendum: user answers (Windows, Studio 21.1) and OTIO target_url forms
- Relative layout confirmed: exports in ...\Downloads\llm-vlog-drafter\spike\out\interchange, media in ...\tests\fixtures\generated. The relative `../../../tests/fixtures/generated/x.mp4` is what the user's Resolve could not resolve.
- Is `file:///C:/...` or `C:\...` accepted? UNVERIFIED for Windows. No Blackmagic or OTIO source found says so (web search came back empty on it).
- Best evidence: samuelgursky docs/reference/api-limitations.md line 467 (measured on Resolve 19.1.3.7, macOS): Resolve's OWN OTIO export writes "a BARE path in `target_url`, not a file:// URL". It also expects Clip.2 with a `media_references` map, an `available_range` per reference, and source_range frames that are timecode-absolute (e.g. 86400 for 01:00:00:00 media). Hand-authored OTIO that misses the shape gets "NO timeline and returning None". So the safest absolute form to try first is the bare native path (`C:\Users\...\x.mp4` or `C:/Users/...`), then `file:///C:/...`.
- Our exporter writes Clip.2 and media_references (seen in phase0.otio), but the timecode-absolute source frames are untested, so OTIO may still hit other problems once paths are fixed.
- Test plan on the user's machine: post-process the OTIO into three variants (bare backslash path, bare forward-slash path, file:///C:/ URL), import each with tools/resolve_verify.py and compare the "media not found" count.
- macOS: Resolve's own export uses a bare POSIX path (same source); Resolve on macOS accepts XML pathurl `file://localhost/...`.
- Windows + Studio 21.1 recommendation: FCP7 XML primary (already clean). Studio means DaVinciResolveScript works, so a deterministic adapter (strategy b) is feasible. The built-in ResolveMCP.exe is Windows-visible and stdio, but is a Python escape hatch, so use it only through a fixed reviewed script or skip it. Windows Python note: Resolve 21.1 bundles ResolvePython 3.14 (per issue 263); the external Python must match what fusionscript supports (samuelgursky suggests 3.10-3.12 as lowest risk).
