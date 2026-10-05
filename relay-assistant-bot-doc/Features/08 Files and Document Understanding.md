# Feature 08: Files and Document Understanding

## Статус

Идея и базовый дизайн, 2026-10-03. Сейчас bot indexing специализирован на images/videos; PDF, офисные документы и прочие файлы не имеют полноценного общего ingestion/extraction flow.

Зависимости/связи: [[07 Chat Search and Source Links]], existing OCR/summary/question modules, [[12 Product UX and Portfolio]].

## Пользовательская ценность

Можно прислать документ и попросить прочитать его, кратко объяснить, найти конкретные данные или ответить на вопрос с указанием страницы. Файл становится доступным последующему поиску в чате.

## Предлагаемый MVP

1. Пассивно индексировать metadata Telegram documents: имя, MIME, размер, `file_id`, `file_unique_id`, chat/message IDs и caption.
2. Читать один replied PDF по явной команде, предварительно `/file`, `/read` или `/document`; название ещё не выбрано.
3. Извлекать встроенный текст по страницам; если текст отсутствует, явно сообщать «скан, нужен OCR».
4. Показывать краткий результат и source link; ответ по содержимому цитирует страницу/фрагмент.
5. Отдельным этапом добавить bounded OCR сканов через existing vision capabilities.

Поддержку TXT/Markdown/DOCX рассмотреть после PDF; XLSX, презентации, архивы и arbitrary binary formats не включать автоматически в первый релиз.

## Extraction contract

```text
Telegram file metadata → explicit download → type/size/page checks
                       → extractor → page/section text chunks
                       → summary/question/search → source citations → cleanup
```

- `DocumentExtractor` возвращает тип, страницы/секции, текст, quality flags и extraction version.
- Связь каждого fragment с `(chat_id, source_message_id, page/section)` обязательна; model-generated answer не становится оригинальным документом.
- `file_unique_id` пригоден для идентичности, но не для скачивания. Cached extraction также учитывает parser/OCR version и settings.
- Metadata и извлечённый текст хранить отдельно от messages, с явным provenance и идемпотентной migration.
- File/network/parser work вынести из event loop в async I/O, thread или bounded subprocess; OCR/LLM используют текущий shared `gpu_lock`.
- Лимиты bytes/pages/extracted chars/runtime применяются до тяжёлого анализа. Сжатые/парольные/повреждённые PDF дают понятный terminal outcome.
- Recognition failure не должен терять metadata; persistence failure не должен скрывать уже готовый ответ.

## Search integration

Feature 07 умеет искать metadata и затем extracted text. Snippet содержит имя файла, страницу и ссылку на оригинальное сообщение. Одинаковый файл, присланный дважды, может переиспользовать extraction, но ссылки остаются привязаны к сообщениям текущего чата.

## Критерии приёмки

- Text PDF читается без OCR и возвращает source-backed summary.
- Скан распознаётся как требующий OCR; нет ложного «документ пуст» при наличии изображений страниц.
- Номер страницы и цитата отвечают реальному extraction, а не модельному предположению.
- Слишком большой/повреждённый/защищённый файл не блокирует polling бесконечно.
- Повторный запрос переиспользует корректный cache; temp files удаляются после success/failure/cancellation.
- Unsupported format имеет явное объяснение, а не попытку читать binary как текст.

## Метрики и открытые вопросы

Измерять extraction success по типам файлов, time-to-answer, долю page-cited answers и точность на подготовленном наборе документов. Targets установить после baseline.

- Какие реальные форматы и размеры чаще встречаются у целевой аудитории?
- OCR запускать вручную или автоматически только для selected PDF?
- Хранить raw files или только metadata + extracted text? Каков retention?
- Нужен ли отдельный вопрос по файлу или reply `/question` с document context?
- Какие parser dependencies станут optional extras для лёгкой установки?
