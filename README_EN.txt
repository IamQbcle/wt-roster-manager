War Thunder Roster Manager
===========================

A free local utility for War Thunder players. It adds an external planning layer on top of the game: track your hangar, store unlimited lineups, rebuild setups more easily, plan progression, and compare compatible lineups with a friend.

War Thunder itself limits the number of in-game presets. When you play many BRs, nations and modes, it becomes easy to forget which crew was trained for which vehicle and what exactly an old setup looked like. War Thunder Roster Manager helps you keep those plans outside the game and restore them later without guessing where each vehicle should go.

This project is not affiliated with Gaijin Entertainment and is not an official War Thunder tool. It does not modify the game client, automate battles, or access your War Thunder account.

The program is completely free: no purchase, activation, subscription, or paid license is required.

Quick start
-----------

1. Unzip the archive into a separate folder.
2. Run Launch App.bat.
3. The program starts a local server at http://127.0.0.1:8765/ and opens the app in your browser.
4. Use the app in the opened browser tab.
5. When you are done, close the browser tab and the WT Roster Local Server window. If it is minimized, find it on the taskbar and close it.
6. You do not have to update the database yourself. The public ZIP already includes the data folder current at the time of the build.
7. After major War Thunder updates, if vehicle BRs, ranks or lineups have changed, the simplest option is to download a newer GitHub release: the maintainer will periodically update and refresh the public build.
8. Manual updating is also possible: run update_from_api.bat. Before doing that, it is recommended to close the app tab and the old local server window to avoid stale browser state.
9. Running and updating require Python 3.9 or newer. On Windows, enable Add python.exe to PATH when installing Python.

The app works out of the box: the archive already includes the data folder with a vehicle database current at the time of the build. The app cannot read your real in-game hangar automatically, so owned vehicles must be marked manually.

Most users do not need to touch the user_data folder manually: the browser and the local server save app data automatically. However, this folder is useful as a portable profile. If you move to another PC, reinstall Windows, or want a backup, keep user_data/wt_roster_user_data.json. It contains ownership marks, lineups, crew roles and settings. Do not publish this file.

Why this exists
---------------

The game has lineups, but the number of in-game presets is limited, and rebuilding old setups can be painful: which crew was trained for which vehicle, what was in each slot, what BR the lineup used, and how to restore everything after experimenting. War Thunder Roster Manager solves this by letting you keep as many lineup plans as you want and quickly understand where each vehicle should go.

It is especially useful if you do not simply rush top tier, but return to different ranks, BRs and modes, build themed lineups, play with friends, or want to use owned vehicles that have been sitting unused.

Main features
-------------

- Personal roster: mark owned vehicles, talismans and planned purchases.
- Unlimited external lineups: store setups for different BRs, modes, nations, events, friends and moods without the in-game preset limit.
- Auto-pick lineups: fill crew slots by rules, considering BR, rank, ownership, acquisition type and crew roles.
- Progression planning: auto-pick can show which vehicles are missing for current or future lineups, helping you decide what to research next instead of grinding everything blindly.
- Premium and rare vehicle control: premium, pack, event, squadron and market vehicles are used by default only if you marked them as owned.
- Find owned vehicles not used in any lineup: useful for discovering vehicles you have not played yet.
- Crew roles: dedicate crews to bombers, attackers, SPAA, heavy tanks and other roles.
- Random lineup choice: “Roulette” chooses a ready lineup for battle when you want variety.
- Squad planning with a friend: import your friend's lineup profile and find compatible lineups without servers, accounts or registration.
- Change history: after updating the database, review BR, rank, class and tree-placement changes.

Roster
------

The “Roster” tab shows War Thunder vehicles as a table or research tree.

You can:

- search vehicles by name;
- filter by nation, class, mode, BR, rank and acquisition type;
- mark vehicles as owned;
- mark talismans in table view;
- add vehicles to lineups.

The “Show vehicles not in lineups” option highlights owned vehicles that are not used in any lineup. This helps you find vehicles you have not played or forgot to include.

If unavailable vehicles are not hidden, the roster can also show rare, hidden and owner-only vehicles. “Owner-only” means available only to players who already own it: new players usually cannot obtain the vehicle, but older owners may still have it in their hangar.

Lineups
-------

The “Lineups” tab stores playable sets. A lineup has:

- title;
- comment;
- nation;
- battle mode;
- BR range;
- rank range;
- up to 11 crew slots;
- “Built in game” flag.

Crew-slot search prioritizes vehicles matching the lineup filters. Vehicles outside BR/rank filters remain searchable for deliberate lower-BR backup use and receive the “FILTER-” tag. This is only a visual hint: you can build a lineup from any vehicle available for the selected battle mode; the app does not artificially block you.

Owner-only vehicles are not suggested in crew slots unless marked as owned. If you own such a vehicle, find it in the “Roster” tab first, disable unavailable-vehicle hiding if needed, mark it as owned, and add it to a lineup from there.

Crew roles
----------

Each crew slot can have a preferred role, for example:

- fighter;
- bomber;
- attacker;
- light tank;
- heavy tank;
- SPAA;
- destroyer.

Crew role preferences are shared by “nation + mode + vehicle class + crew slot number”. For example, if USA Air AB crew #10 is set as a bomber crew, the same preference is reused in other USA Air AB lineups.

Auto-pick
---------

“Auto-pick” fills the selected lineup according to your rules. It is useful when you want to quickly build a lineup for a specific BR, try vehicles you would not normally choose manually, or plan a future lineup around vehicles you have not researched yet.

Its strongest use is progression planning: you can build target lineups in advance and see exactly what you need to research or buy in game, instead of grinding everything blindly.

Auto-pick can consider:

- nation;
- mode;
- BR;
- rank;
- vehicle class;
- ownership;
- acquisition type;
- preferred crew roles.

Each acquisition type has a policy:

- use only if owned;
- use all;
- do not use.

Acquisition types:

- regular;
- premium;
- pack;
- event;
- squadron;
- market.

By default, regular vehicles are allowed, while premium, pack, event, squadron and market vehicles are used only if marked as owned. This prevents auto-pick from suggesting purchases for random slots.

Ownership priority can also be set separately: owned first, equal chance, owned only or unowned only.

Auto-pick softly tries to place vehicles into crews with matching preferred roles. This is not a hard lock: if no exact role match is available, a slot can still be filled.

Roulette
--------

“Roulette” randomly chooses a lineup for battle. It is useful when you want variety instead of always playing the same lineup.

Squad
-----

The “Squad” tab helps compare lineups with a friend without servers, accounts or online sync.

How to use it:

1. Your friend downloads this program from GitHub.
2. Your friend creates or imports their lineups.
3. Your friend exports their squad profile.
4. You import your friend's profile.
5. Choose compatibility filters: mode, BR tolerance, “Built in game” flag and nation.
6. The app shows compatible lineups.
7. Squad “Roulette” can randomly choose a compatible pair or group.

Imported friend profiles are read-only and do not merge into your main save.

Changes
-------

The “Changes” tab shows what changed after a database update: BR, rank, class, tree placement, added vehicles or missing vehicles.

If a change affects a vehicle used in your lineups, it helps you quickly review the affected lineup.

Updating data
-------------

There are two reliable ways to keep the database current:

1. Download a newer version from GitHub, where the maintainer has already updated the data folder.
2. Run update_from_api.bat to update the data yourself.

For most users, downloading the latest release is easier. Manual updating is useful if you want to check changes immediately after a patch without waiting for a new public build.

Vehicle availability
--------------------

Acquisition type and availability are not the same thing. A vehicle can historically be a regular tree vehicle but still be hidden for new players. The database therefore keeps separate availability logic.

Rare, hidden and owner-only cases are maintained manually after source review. This is safer than trying to automatically judge availability from marketing news or store pages.

Windows, Linux and macOS compatibility
--------------------------------------

War Thunder is officially available for Windows, Linux and macOS. Steam lists Linux requirements, and the official War Thunder site lists macOS requirements for Big Sur 11.0 or newer.

This app itself is an HTML interface, a local Python server and JSON files. In principle, it can work not only on Windows, but also on Linux/macOS with Python 3.9+ and a modern browser.

The main tested scenario right now is Windows through Launch App.bat. Launch_App.sh and update_from_api.sh are included for Linux/macOS, but those workflows need testing by users of those systems.

Under the hood
--------------

Main files:

- index.html — app UI and main logic;
- scripts/local_server.py — local 127.0.0.1:8765 server and user_data saving;
- scripts/update_from_api.py — vehicle database updater;
- data/vehicles.json — normalized vehicle database;
- data/vehicles_api_raw.json — raw API response;
- data/tech_tree_layout.json — research tree layout;
- data/availability_overrides.json — manual availability overrides;
- data/patch_history.json — change history;
- user_data/wt_roster_user_data.json — your personal app data.

Base data source: WarThunder Vehicles API
https://github.com/Sgambe33/WarThunder-Vehicles-API

War Thunder Wiki data is also used for display names, links, research tree layout and vehicle details.

Limitations
-----------

- The app cannot automatically read your real in-game hangar. Owned vehicles must be marked manually.
- This is a local utility, not an online service.
- Squad mode works through file export/import.
- Rare vehicle and pack availability may require manual checking.
- The app does not modify the War Thunder client and does not automate gameplay.

Participation and feedback
--------------------------

Forks, ideas, criticism, bug reports and improvement suggestions are welcome. The program was made by a non-professional with help from ChatGPT, so there may be rough edges, mistakes and room for improvement.

Do not publish
--------------

Do not publish:

- user_data/wt_roster_user_data.json;
- private friend squad profiles unless intended;
- temporary logs and caches if they appear locally.

--- v3.76 ---
- Added browser favicon / tab icon.
- Launch App.bat and update_from_api.bat now show a friendlier Python-missing notice and offer to open the Python download page via Y/N.
- Added first-run onboarding inside the app with a “do not show again” checkbox and a manual replay button in Settings.
- Added a manual game-patch review status to the Changes tab: fresh public signals can tint the tab pale red until the user acknowledges that the data is still current.

--- v3.77 ---
- Added an in-app button on the Changes tab to check the latest public War Thunder version from the official changelog.
- Only the game version number is checked; news, store items, BR and availability are not analyzed automatically.
- If the found version differs from the verified data version, the Changes tab is highlighted.
