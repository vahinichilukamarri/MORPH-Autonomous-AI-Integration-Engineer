/** The MORPH mark: two offset nodes joined by a bending path, the integration it builds. */
export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg className="brand__mark" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="1" y="1" width="22" height="22" rx="7" className="brand__tile" />
      <path d="M7 15.5c0-4 2.5-4 5-4s5 0 5-4" fill="none" className="brand__path" strokeWidth="2" strokeLinecap="round" />
      <circle cx="7" cy="16" r="2.4" className="brand__node" />
      <circle cx="17" cy="8" r="2.4" className="brand__node brand__node--alt" />
    </svg>
  )
}

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="brand">
      <BrandMark />
      {!compact && <span className="brand__name">MORPH</span>}
    </span>
  )
}
