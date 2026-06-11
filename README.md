# WT Roster Manager

## 🇬🇧 English

**WT Roster Manager** is a free local roster, lineup and preset planning tool for War Thunder.

It adds an external planning layer on top of the game: unlimited lineups, owned-vehicle tracking, auto-pick, lineup roulette, squad compatibility and collection planning.

War Thunder has a limited number of in-game presets, and crews are trained for specific vehicles. This tool helps you keep many external lineups, rebuild old presets later, plan future research and avoid guessing which vehicle was supposed to be assigned to which crew.

## 🇷🇺 Русский

**WT Roster Manager** — бесплатный локальный менеджер ангара, коллекции и наборов для War Thunder.

Он добавляет внешний слой планирования поверх игры: неограниченное количество наборов, учёт купленной техники, автоподбор, рулетку наборов, проверку совместимости для игры отрядом и планирование коллекции.

В War Thunder ограничено количество игровых наборов, а экипажи обучаются на конкретную технику. Эта программа помогает хранить много внешних наборов, восстанавливать старые пресеты, планировать прокачку и не держать в голове, какая техника должна стоять на каком экипаже.

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

The repository also contains the full working project files, but the release ZIP is the easiest way to download the app.

В репозитории также лежат рабочие файлы проекта, но ZIP из Releases — самый простой способ скачать программу.

Use the latest ZIP from the [**Releases**](https://github.com/IamQbcle/wt-roster-manager/releases) section.

Используйте свежий ZIP из раздела [**Releases**](https://github.com/IamQbcle/wt-roster-manager/releases).

<a id="quickstart"></a>

## 🧭 Quickstart / Быстрый старт

### 🇬🇧 English

1. Install Python 3 from python.org if it is not installed yet. During installation, enable **Add python.exe to PATH**.
2. Download the latest ZIP from the **Releases** section.
3. Extract the whole archive to a normal folder.
4. Run `Launch App.bat`.
5. Open the roster and mark your owned vehicles.
6. Recreate your current in-game lineups in the app.
7. Now you can plan new lineups, research goals, squad play and lineup roulette without losing your saved plans.

Detailed instructions are available in [README_EN.txt](README_EN.txt) and [README_RU.txt](README_RU.txt).

### 🇷🇺 Русский

1. Установите Python 3 с python.org, если он ещё не установлен. При установке включите галочку **Add python.exe to PATH**.
2. Скачайте свежий ZIP из раздела **Releases**.
3. Полностью распакуйте архив в обычную папку.
4. Запустите `Launch App.bat`.
5. Откройте ростер и отметьте свою купленную технику.
6. Создайте в программе ваши текущие игровые наборы.
7. Теперь можно планировать новые наборы, порядок исследования, игру отрядом и рулетку наборов, не теряя сохранённые планы.

Подробное описание функций есть в [README_RU.txt](README_RU.txt) и [README_EN.txt](README_EN.txt).

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

### 🇬🇧 English

The main tested launch method is Windows: `Launch App.bat`.

Experimental Linux/macOS scripts are included:

- `Launch_App.sh`
- `update_from_api.sh`

They may require manual permission changes, for example:

```bash
chmod +x Launch_App.sh update_from_api.sh
```

Linux/macOS feedback and fixes are welcome.

### 🇷🇺 Русский

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

The vehicle database is built with help from the community War Thunder Vehicles API:

https://github.com/Sgambe33/WarThunder-Vehicles-API

Some availability corrections are maintained manually when the public API cannot fully reflect hidden, owner-only or removed vehicles.

База техники собирается с помощью стороннего War Thunder Vehicles API и данных War Thunder Wiki. Часть статусов доступности поддерживается вручную, если публичные источники не могут точно отразить скрытую, удалённую или доступную только владельцам технику.

<a id="safety"></a>

## Safety / Безопасность

### 🇬🇧 English

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

### 🇷🇺 Русский

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
