# kb-mcp Design System

Working tool for searching and maintaining a knowledge base. Calm, dense where it helps, one accent.
Dominant move: crisp type hierarchy on quiet neutrals; the accent marks only the primary action, links and focus.
All values live as CSS custom properties in `kb/static/app.css` (`:root`, dark override under `prefers-color-scheme: dark`). Components read tokens only.

## Colors (light / dark)
- **bg** `#f6f7f9` / `#0f1217`: page background
- **surface** `#ffffff` / `#171b22`: cards, header, inputs
- **surface-2** `#f1f3f6` / `#1e232c`: table header, code, tag chips, hover rows
- **border** `#e3e6eb` / `#2a313c`: card and input borders
- **border-strong** `#c9ced6` / `#3a4250`: hovered borders
- **text** `#14181f` / `#e6e9ee`: body text
- **muted** `#5b6472` / `#98a2b1`: secondary text, labels, meta (AA on bg and surface)
- **accent** `#2459d6` / `#7aa2ff`: primary buttons, links, focus ring, active nav
- **accent-hover** `#1c48b3` / `#9bb9ff`
- **ok / warn / bad / info**: tinted bg + strong fg pairs, used only for status (confidence, notices, token state)

## Typography
- Sans: system UI stack. Mono: `ui-monospace, SFMono-Regular, Menlo, Consolas` for scores, dates, IDs, tokens, code.
- Scale: 28/1.2 600 (page title), 20/1.3 600 (card title), 17/1.35 600 (result title), 16/1.55 400 (body), 14/1.45 (meta, labels), 12/1.3 600 (badges, uppercase tracking 0.04em).

## Spacing
Base 4px. Scale used: 4, 8, 12, 16, 20, 24, 32, 48.

## Radius and elevation
- Radius: 6px (inputs, buttons, badges are pills), 10px (cards).
- Elevation: `--shadow-1` hairline for cards; `--shadow-2` for hovered result cards. No other shadows.

## Components
- **Button**: 36px tall, 6px radius. Primary (accent fill, white text), secondary (surface, border), quiet (text only). States: hover, active (translateY 1px), focus-visible (2px accent ring, 2px offset), disabled (55% opacity).
- **Input**: 36px tall, surface background, border; focus shows accent border and a 3px tinted ring. Visible labels above fields.
- **Card**: surface, 1px border, 10px radius, 20px padding.
- **Result**: card with title link, path line, 3-line clamped snippet, mono meta row. Hover raises border and shadow.
- **Badge**: pill, 12px uppercase; status colors only.
- **Table**: sticky-looking header row on surface-2, row hover, wraps in a horizontally scrollable card on narrow screens.
- **Notice**: tinted block with a 3px left rule in the status color.

## Motion
- Durations: fast 120ms, base 200ms. Curves: `--ease-out: cubic-bezier(0.16, 1, 0.3, 1)` (glide).
- Allowed: hover/press feedback, focus ring, results fade-and-rise on swap (staggered 30ms, max 6 items), spinner while a request runs.
- Not allowed: parallax, marquees, scroll effects, 3D. Everything off under `prefers-reduced-motion: reduce`.

## Guidelines
- One primary (filled) button per form. Secondary actions use the default button.
- Never color-only status: badges always carry words.
- Copy is short and direct: "Search", "Add", "Verify". Errors say what to do next.
- No new dependencies, no CDN, no inline styles or scripts (strict CSP).
