# Asana Connector — UI component plan

Источники: `ui-primitives-reference.md`, `UI_INTERFACE_STANDARD.md`, `concepts/panels.md`.
Основано на `POST_CONNECT_EXPERIENCE.md` этого приложения.

## 1. Компоненты

| Экран | Примитивы | Почему именно эти |
|---|---|---|
| Sidebar (left) | `ui.Column`(align="start") + `ui.Text`(workspace) + `ui.Divider` + navigation `ui.ListItem`(Projects/My Tasks/Portfolios) + `ui.Button`("App settings") | Без карточек по стандарту. |
| Project Board (center, `center_overlay=True`) | `ui.Stats`(Total/Overdue/Done this week) + `ui.Tabs`(List/Board view) → `ui.DataTable` для List; для Board — `ui.Row`(колонки-секции через N×`ui.Column`, внутри каждой `ui.List`(задачи этой секции как ListItem)) | В SDK нет отдельного Kanban-примитива (см. `UI_COMPONENT_VOCABULARY.md` §4) — доска собирается из `Row` колонок-секций, каждая — обычный `List`; перемещение задачи между секциями — через `Select`(секция) на карточке задачи (drag-and-drop не задокументирован в SDK-справочнике). |
| Task Detail | Back-button + `ui.KeyValue`(assignee/due date/project) + `ui.List`(subtasks как ListItem с `ui.Toggle` в качестве "выполнено"-чекбокса на каждой строке) + `ui.Timeline`(comments/activity log) + `ui.TextArea`(param_name="comment", placeholder="Написать комментарий...") | В SDK нет отдельного Checklist (см. `UI_COMPONENT_VOCABULARY.md` §4) — список subtasks с бинарным состоянием собирается из `List` + `Toggle` на каждом ListItem. |
| My Tasks (across projects) | `ui.DataTable`(task, project, due date, priority Badge; sortable, group_by=due_date) | Личный сборный список задач пользователя из разных проектов. |
| Portfolio Overview | `ui.Chart`(type="bar" — % complete по проекту) + `ui.DataTable`(project, owner, status, deadline) | Обзор нескольких проектов для руководителя портфеля. |
| Timeline/Gantt View | `ui.Timeline` per проект с date range в title каждого элемента | В SDK нет отдельного GanttChart (см. `UI_COMPONENT_VOCABULARY.md` §4) — зависимости между задачами показываются через `Timeline` с датами начала/окончания в описании каждой задачи, не как настоящая шкала времени. |
| App Settings | `ui.Accordion`([Connections+Disconnect, Default Workspace Select, Webhooks CRUD]) | Централизованные настройки по стандарту. |

## 2. User flow

1. **SESSION INIT** → `__panel__asana_sidebar` рендерит workspace + разделы,
   `auto_action` открывает "Мои задачи".
2. My Tasks: DataTable, сгруппированная по due date → клик на задачу →
   `ui.Call(task_id=...)` → Task Detail на том же center handler.
3. Task Detail: List subtasks с Toggle-чекбоксом (клик по Toggle → `ui.Call(action=toggle_subtask)`
   → `refresh_panels=["asana_task_detail"]`), Timeline активности, TextArea комментария
   → Button "Отправить" → `refresh_panels=["asana_task_detail"]`.
4. Раздел "Projects" → выбор проекта → Project Board (Tabs List/Board) → в Board-виде
   перемещение задачи между секциями через `Select` секции на карточке задачи (`on_change`
   вызывает `move_task_to_section` → `refresh_panels`).
5. Раздел "Portfolios" → Chart % complete по проектам → клик на проект → Project
   Board этого проекта.
6. "App settings" → Accordion: Connections, Default Workspace, Webhooks.

## 3. Конкретные экраны (screens)

### Screen: My Tasks (`asana_my_tasks`, default)
- Stats row: `Total`, `Overdue`, `Done this week`.
- DataTable (group_by due date): task, project, priority Badge — row-click → Task
  Detail.

### Screen: Task Detail (`asana_my_tasks` + `task_id`)
- Back-button "← К задачам".
- KeyValue: assignee, due date, project, priority.
- List (subtasks-как-ListItem) c Toggle-чекбоксом на каждой строке.
- Timeline: комментарии и activity log.
- Внизу: TextArea "Комментарий" (placeholder "Написать комментарий..."), Button
  "Отправить".

### Screen: Project Board (`asana_project` + `project_id`)
- Tabs: "Список" (DataTable) / "Доска" (колонки по секциям с карточками задач).
- Кнопка "Добавить задачу" вверху → inline Form (`ui.Input`(title) + `ui.Select`(assignee)).

### Screen: Portfolios (`asana_portfolios`)
- Chart (bar): % complete по каждому проекту.
- DataTable: project, owner, status Badge, deadline — row-click → Project Board.

### Screen: App settings (`asana_settings`)
- Accordion "Подключение": workspace, Rotate/Disconnect (Dialog-подтверждение).
- Accordion "Рабочее пространство по умолчанию": Select.
- Accordion "Webhooks": List + Button "Добавить".
