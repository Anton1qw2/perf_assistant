# Семантика состояния подключений

Каждая проверка хранится на сервере: check ID, source, revision настроек, scope, checked/expiry time, `synthetic`, отдельные stage outcomes и code-owned reason codes. Web, TUI и чат читают одни записи.

Configuration pass означает только наличие конфигурации. Protocol pass означает успешный MCP initialize/list. Permitted-tools pass означает пересечение каталога с allowlist. Authorization pass устанавливается только после успешного проверяемого чтения, а public discovery оставляет её unknown. Selected-object pass без identity mapping означает принятие сконфигурированного чтения, но не независимое подтверждение ID. Missing/forbidden не разделяется по произвольному тексту ошибок. Git commit readability не устанавливает соответствие профиля этому build.

Статусы: не проверено, проверка, частично, доступ проверен, ошибка/отказ, устарело. Expiry не является fail. Результат старой settings revision не заменяет текущий и не создаёт ответ, выдающий его за актуальный. Модель не участвует в формировании диагностических ответов.

При старте UI только читает сохранённые записи. Явные команды `/check [all|profiler|git|docs]`, RU/EN диагностические запросы и кнопки ставят задания в очередь. Эквивалентные одновременные запросы дедуплицируются. Никаких code execution, collect profile, remote documentation crawling, clone/fetch или изменений конфигурации проверки не выполняют.

При отсутствии сервера показывается `not_configured`, а не синтетическое подключение. Fixtures тестов изолированы в test storage; приложение не содержит режима, выдающего demo-проверки за реальные.
