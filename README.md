# WT Roster Manager

A free local roster, lineup and collection-planning tool for **War Thunder**.

War Thunder now supports up to **25 in-game presets**, which is enough for most players. WT Roster Manager is therefore not mainly about adding more preset slots. Its main purpose is to provide the planning, memory and collection tools that are difficult to keep inside the game itself: crew-training memory, future lineup planning, smart auto-pick, correct mixed-mode aircraft BRs, patch-change tracking and squad compatibility.

## EN — English

### What the app adds beyond in-game presets

- **Crew Training Memory** — remembers which owned vehicles have appeared on each crew in your saved lineups and uses that history for slot hints and auto-pick priorities.
- **Personal roster and collection tracking** — mark owned vehicles and talismans, plan future purchases/research, and find owned vehicles that are not used in any lineup.
- **Smart lineup planning and auto-pick** — build lineups by nation, mode, BR, rank, ownership, acquisition type and crew role. Planned/unowned vehicles can be used for future setups without being treated as already trained.
- **Correct aircraft BR handling for mixed modes** — Ground/Naval RB and Ground SB can use the aircraft's combined-mode BR instead of blindly reusing Air RB/SB values.
- **Patch change tracking** — BR changes are treated as actionable and can show affected lineups; rank, availability, added vehicles and tree changes remain available as informational history.
- **Squad planning** — import a friend's exported lineup profile and find compatible lineups without accounts, servers or online sync.
- **Unlimited external archive and future plans** — keep extra, experimental or historical lineups beyond the game's 25 presets without constantly rebuilding them in War Thunder.
- **Lineup roulette** — randomly choose a ready lineup, including compatible squad combinations, when you want variety.

The app is local and does **not** read your War Thunder account or game client. Owned vehicles are marked manually.

## RU — Русский

### Что программа даёт сверх игровых пресетов

В самой War Thunder теперь доступно до **25 игровых пресетов**, и для большинства игроков этого количества вполне достаточно. Поэтому главный смысл WT Roster Manager уже не в том, чтобы просто дать «ещё больше наборов». Программа полезна как отдельный слой планирования, памяти и работы с коллекцией.

- **Память обученности экипажей** — программа запоминает, какая купленная техника стояла на конкретных экипажах в сохранённых наборах, показывает подсказки и учитывает это при автоподборе.
- **Личный ростер и коллекция** — можно отмечать купленную технику и талисманы, планировать будущие покупки/исследование и находить купленные машины, которые не используются ни в одном наборе.
- **Умное планирование и автоподбор** — наборы собираются с учётом нации, режима, БР, ранга, наличия техники, типа получения и ролей экипажей. Можно заранее собирать будущие наборы из ещё не исследованной техники.
- **Корректные БР авиации в смешанных режимах** — для наземных/морских РБ и наземных СБ учитываются соответствующие комбинированные БР самолётов, а не только значения воздушных режимов.
- **История изменений после патчей** — изменения БР считаются требующими внимания и могут показывать затронутые наборы; изменения ранга, доступности, добавление техники и перестановки дерева остаются информационной историей.
- **Планирование игры отрядом** — можно импортировать профиль наборов друга и найти совместимые варианты без аккаунтов, серверов и регистрации.
- **Неограниченный внешний архив и будущие планы** — дополнительные, экспериментальные и старые наборы можно хранить вне игры сверх 25 игровых пресетов и восстанавливать при необходимости.
- **Рулетка наборов** — случайный выбор готового набора или совместимого варианта для отряда, если хочется разнообразия.

Программа работает локально и **не читает** ваш аккаунт или клиент War Thunder. Купленную технику пользователь отмечает вручную.

## Contents / Оглавление

- [Documentation / Документация](#documentation)
- [Download / Скачать](#download)
- [Quickstart / Быстрый старт](#quickstart)
- [Screenshots / Скриншоты](#screenshots)
- [Feedback / Обратная связь](#feedback)
- [Platform notes / Платформы](#platform-notes)
- [Data source / Источник данных](#data-source)
- [Safety / Безопасность](#safety)
- [Disclaimer / Дисклеймер](#disclaimer)

<a id="documentation"></a>

## ⭐ Documentation / Документация

- [English documentation](README_EN.txt)
- [Русская документация](README_RU.txt)

<a id="download"></a>

## 🚀 Download / Скачать

The repository contains the full working project files, but the release ZIP is the easiest way to download the app.

В репозитории лежат полные рабочие файлы проекта, но ZIP из Releases — самый простой способ скачать программу.

Use the latest ZIP from the [**Releases**](https://github.com/IamQbcle/wt-roster-manager/releases) section.

Используйте свежий ZIP из раздела [**Releases**](https://github.com/IamQbcle/wt-roster-manager/releases).

<a id="quickstart"></a>

## 🧭 Quickstart / Быстрый старт

### EN — English

1. Install Python 3 from python.org if it is not installed yet. During installation, enable **Add python.exe to PATH**.
2. Download the latest ZIP from the **Releases** section.
3. Extract the whole archive to a normal folder.
4. Run `Launch App.bat`.
5. Open the roster and mark your owned vehicles.
6. Recreate the in-game lineups you want the app to remember.
7. Use the roster, crew-training hints, auto-pick, future plans, Changes tab, roulette and squad tools as needed.

Detailed instructions are available in [README_EN.txt](README_EN.txt) and [README_RU.txt](README_RU.txt).

The release ZIP already includes the vehicle database and images. `update_from_api.bat` is optional and is only needed for a manual database rebuild/update. In some regions, the third-party API domain may require a VPN.

### RU — Русский

1. Установите Python 3 с python.org, если он ещё не установлен. При установке включите галочку **Add python.exe to PATH**.
2. Скачайте свежий ZIP из раздела **Releases**.
3. Полностью распакуйте архив в обычную папку.
4. Запустите `Launch App.bat`.
5. Откройте ростер и отметьте свою купленную технику.
6. Создайте в программе те игровые наборы, историю экипажей которых хотите сохранить.
7. Дальше можно пользоваться ростером, подсказками обученности экипажей, автоподбором, будущими планами, вкладкой «Изменения», рулеткой и инструментами отряда.

Подробное описание функций есть в [README_RU.txt](README_RU.txt) и [README_EN.txt](README_EN.txt).

ZIP-релиз уже содержит базу техники и изображения. `update_from_api.bat` не обязателен для обычного запуска и нужен только для ручного обновления/пересборки базы. В некоторых регионах домен стороннего API может быть доступен только через VPN.

<a id="screenshots"></a>

## 🖼️ Screenshots / Скриншоты

<p align="center">
  <img src="screenshots/roster.png" width="32%" alt="Roster / Ростер">
  <img src="screenshots/lineup-editor.png" width="32%" alt="Lineup editor / Редактор наборов">
  <img src="screenshots/autopick.png" width="32%" alt="Auto-pick / Автоподбор">
</p>

<details>
<summary><strong>Open full screenshots / Открыть все скриншоты</strong></summary>

### Roster / Ростер

![Roster](screenshots/roster.png)

### Lineup editor / Редактор наборов

![Lineup editor](screenshots/lineup-editor.png)

### Auto-pick / Автоподбор

![Auto-pick](screenshots/autopick.png)

### Squad compatibility / Совместимость отряда

![Squad compatibility](screenshots/squad.png)

</details>

<a id="feedback"></a>

## 💬 Feedback / Обратная связь

Questions, ideas and bug reports / Вопросы, идеи и багрепорты:

[GitHub Discussions](https://github.com/IamQbcle/wt-roster-manager/discussions)

<a id="platform-notes"></a>

## Platform notes / Платформы

### EN — English

The main tested launch method is Windows: `Launch App.bat`.

Experimental Linux/macOS scripts are included:

- `Launch_App.sh`
- `update_from_api.sh`

They may require manual permission changes, for example:

```bash
chmod +x Launch_App.sh update_from_api.sh
```

Linux/macOS feedback and fixes are welcome.

### RU — Русский

Основной проверенный способ запуска — Windows: `Launch App.bat`.

В архиве также есть экспериментальные скрипты для Linux/macOS:

- `Launch_App.sh`
- `update_from_api.sh`

Возможно, им потребуется вручную выдать права на запуск, например:

```bash
chmod +x Launch_App.sh update_from_api.sh
```

Проверки, багрепорты и исправления для Linux/macOS приветствуются.

<a id="data-source"></a>

## Data source / Источник данных

The vehicle database is built with help from the community War Thunder Vehicles API and War Thunder Wiki data. Some availability corrections are maintained manually when public sources cannot fully reflect hidden, owner-only or removed vehicles.

https://github.com/Sgambe33/WarThunder-Vehicles-API

База техники собирается с помощью стороннего War Thunder Vehicles API и данных War Thunder Wiki. Часть статусов доступности поддерживается вручную, если публичные источники не могут точно отразить скрытую, удалённую или доступную только владельцам технику.

<a id="safety"></a>

## Safety / Безопасность

### EN — English

WT Roster Manager is a local open-source HTML/Python tool.

- It is not an overlay, not a cheat, and not a game-client mod.
- It does not modify or interact with the War Thunder client.
- It does not require administrator rights.
- It is not an `.exe` installer.
- It does not ask for Gaijin, War Thunder, Steam, Discord or any other account credentials, and does not read browser cookies or saved passwords.
- It runs locally through `127.0.0.1:8765`.
- User data is stored locally in `user_data/wt_roster_user_data.json`.
- The main launch/update scripts can be inspected before running:
  - `Launch App.bat`
  - `update_from_api.bat`
  - `scripts/local_server.py`
  - `scripts/update_from_api.py`

If any modified copy of this project asks for passwords, tokens, administrator rights or account access, do not use it.

### RU — Русский

WT Roster Manager — локальная open-source утилита на HTML/Python.

- Это не оверлей, не чит и не модификация клиента игры.
- Программа не изменяет клиент War Thunder и не взаимодействует с ним.
- Программа не требует прав администратора.
- Это не `.exe`-установщик.
- Программа не спрашивает логин/пароль от Gaijin, War Thunder, Steam, Discord или других аккаунтов, а также не читает cookies браузера и сохранённые пароли.
- Программа работает локально через `127.0.0.1:8765`.
- Пользовательские данные хранятся локально в `user_data/wt_roster_user_data.json`.
- Перед запуском можно проверить основные файлы:
  - `Launch App.bat`
  - `update_from_api.bat`
  - `scripts/local_server.py`
  - `scripts/update_from_api.py`

Если изменённая копия программы просит пароли, токены, права администратора или доступ к аккаунтам — не используйте её.

<a id="disclaimer"></a>

## Disclaimer / Дисклеймер

This is an unofficial fan-made tool.

It is not affiliated with Gaijin Entertainment or War Thunder.

Created by a non-professional developer with ChatGPT assistance.

Это неофициальная фанатская утилита.

Проект не связан с Gaijin Entertainment и War Thunder.

Создано непрофессиональным разработчиком при помощи ChatGPT.
