/** A small stroke icon set, drawn on a 20 px grid. Decorative unless given a label. */
const PATHS = {
  overview: 'M3 3h6v6H3zM11 3h6v4h-6zM11 9h6v8h-6zM3 11h6v6H3z',
  pipeline: 'M3 6h4v4H3zM13 3h4v4h-4zM13 13h4v4h-4zM7 8h3m0 0v-3h3m-3 3v7h3',
  repair: 'M15 4a4 4 0 0 1-5.6 5.6L4 15l1 1 5.4-5.4A4 4 0 0 1 16 5l-2.5 2.5-1-1.5L15 4z',
  results: 'M3 17V9m5 8V4m5 13v-6m5 6V7M2 17h16',
  review: 'M10 2l6 3v5c0 4-2.7 6.7-6 8-3.3-1.3-6-4-6-8V5l6-3zM7 10l2 2 4-4',
  policy: 'M5 9V6a5 5 0 0 1 10 0v3M4 9h12v9H4zM10 12v3',
  search: 'M9 15A6 6 0 1 0 9 3a6 6 0 0 0 0 12zM13.5 13.5L17 17',
  sun: 'M10 14a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM10 1v2M10 17v2M1 10h2M17 10h2M3.6 3.6l1.4 1.4M15 15l1.4 1.4M3.6 16.4L5 15M15 5l1.4-1.4',
  moon: 'M16 12.5A7 7 0 0 1 7.5 4 7 7 0 1 0 16 12.5z',
  keyboard: 'M2 5h16v10H2zM5 8h1M8 8h1M11 8h1M14 8h1M5 12h10',
  github:
    'M10 2a8 8 0 0 0-2.5 15.6c.4 0 .5-.2.5-.4v-1.4c-2.2.5-2.7-1-2.7-1-.4-.9-.9-1.2-.9-1.2-.7-.5.1-.5.1-.5.8.1 1.2.8 1.2.8.7 1.2 1.9.9 2.3.7 0-.5.3-.9.5-1.1-1.8-.2-3.6-.9-3.6-4 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8a7.5 7.5 0 0 1 4 0c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.6.8 1.3.8 2.1 0 3.1-1.9 3.8-3.6 4 .3.3.6.8.6 1.5v2.2c0 .2.1.5.6.4A8 8 0 0 0 10 2z',
  chevronLeft: 'M12 4l-6 6 6 6',
  chevronRight: 'M8 4l6 6-6 6',
  arrowRight: 'M4 10h12m-5-5l5 5-5 5',
  external: 'M8 4H4v12h12v-4M11 3h6v6M17 3l-8 8',
  close: 'M5 5l10 10M15 5L5 15',
  menu: 'M3 5h14M3 10h14M3 15h14',
  command: 'M7 7V5a2 2 0 1 0-2 2h2zm0 0h6m-6 0v6m6-6V5a2 2 0 1 1 2 2h-2zm0 0v6m0 0h2a2 2 0 1 1-2 2v-2zm0 0H7m0 0v2a2 2 0 1 1-2-2h2z',
  check: 'M4 10l4 4 8-8',
  alert: 'M10 3l8 14H2L10 3zM10 8v4M10 14.5v.5',
  home: 'M3 9l7-6 7 6v8h-5v-5H8v5H3z',
} as const

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 18, label }: { name: IconName; size?: number; label?: string }) {
  const filled = name === 'github'
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill={filled ? 'currentColor' : 'none'}
      stroke={filled ? 'none' : 'currentColor'}
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
