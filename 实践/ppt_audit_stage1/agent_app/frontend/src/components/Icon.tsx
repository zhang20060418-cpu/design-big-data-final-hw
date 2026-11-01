import type { SVGProps } from 'react'

type IconName =
  | 'logo' | 'home' | 'history' | 'settings' | 'upload' | 'check' | 'alert' | 'x'
  | 'spinner' | 'zoomIn' | 'zoomOut' | 'fit' | 'eye' | 'eyeOff' | 'left' | 'right'
  | 'play' | 'download' | 'external' | 'cpu' | 'layers' | 'spark' | 'shield' | 'file'
  | 'image' | 'chart' | 'arrow' | 'refresh' | 'trash' | 'dot' | 'info' | 'scan'
  | 'search' | 'bell' | 'chevron' | 'folder' | 'help' | 'bulb'

const paths: Record<IconName, JSX.Element> = {
  logo: (
    <>
      <rect x="3" y="4" width="18" height="13" rx="2.5" />
      <path d="M3 9.5h18M8 20h8M12 17v3" />
      <circle cx="8.2" cy="6.8" r="1" fill="currentColor" stroke="none" />
      <path d="M11 13.2c1.1-1.7 3.8-1.7 4.9 0" />
    </>
  ),
  home: <><path d="M3 10.5 12 3l9 7.5" /><path d="M5.5 9.5V20h13V9.5" /><path d="M10 20v-5h4v5" /></>,
  history: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  settings: <><circle cx="12" cy="12" r="3.2" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.4-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4a2 2 0 1 1 4 0 1.7 1.7 0 0 0 2.9 1.4l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 1 1 0 4Z" /></>,
  upload: <><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16" /></>,
  check: <path d="m5 12.5 4.2 4.2L19 7" />,
  alert: <><path d="M12 4 2.8 20h18.4L12 4Z" /><path d="M12 9.5v4.6" /><circle cx="12" cy="17" r=".9" fill="currentColor" stroke="none" /></>,
  x: <><path d="m6 6 12 12M18 6 6 18" /></>,
  spinner: <><path d="M12 3a9 9 0 1 0 9 9" /></>,
  zoomIn: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 5 5M10.5 7.8v5.4M7.8 10.5h5.4" /></>,
  zoomOut: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 5 5M7.8 10.5h5.4" /></>,
  fit: <><path d="M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4" /></>,
  eye: <><path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.6" /></>,
  eyeOff: <><path d="M4 4l16 16" /><path d="M9.9 5.2A10.5 10.5 0 0 1 12 5c6 0 9.5 7 9.5 7a16 16 0 0 1-2.7 3.5M6.3 7.3A16.7 16.7 0 0 0 2.5 12s3.5 7 9.5 7c1.4 0 2.6-.3 3.7-.8" /><path d="M10.2 10.4a2.6 2.6 0 0 0 3.5 3.6" /></>,
  left: <path d="m14.5 5-7 7 7 7" />,
  right: <path d="m9.5 5 7 7-7 7" />,
  play: <path d="M8 4.5 20 12 8 19.5v-15Z" />,
  download: <><path d="M12 4v11" /><path d="m7.5 11 4.5 4.5L16.5 11" /><path d="M4.5 19.5h15" /></>,
  external: <><path d="M14 4h6v6" /><path d="M20 4 11 13" /><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" /></>,
  cpu: <><rect x="6.5" y="6.5" width="11" height="11" rx="2" /><rect x="10" y="10" width="4" height="4" /><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" /></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 13 9 5 9-5" /><path d="m3 17.5 9 5 9-5" /></>,
  spark: <><path d="M12 3.5 13.7 9l5.5 1.7-5.5 1.7L12 18l-1.7-5.6L4.8 10.7 10.3 9 12 3.5Z" /><path d="M18.5 16.5 19.3 19l2.5.8-2.5.8-.8 2.5" /></>,
  shield: <><path d="M12 3 5 6v5.5c0 4.3 3 7.6 7 9.5 4-1.9 7-5.2 7-9.5V6l-7-3Z" /><path d="m9 12 2 2 4-4" /></>,
  file: <><path d="M6 3h7l5 5v13H6V3Z" /><path d="M13 3v5h5" /><path d="M9 13h6M9 16.5h4" /></>,
  image: <><rect x="3.5" y="5" width="17" height="14" rx="2" /><circle cx="8.5" cy="10" r="1.5" /><path d="m5 17 4.5-4.5 3 3 2.5-2.5L19 17" /></>,
  chart: <><path d="M4 20V4" /><path d="M4 20h16" /><rect x="7" y="11" width="3" height="6" /><rect x="12" y="8" width="3" height="9" /><rect x="17" y="13" width="3" height="4" /></>,
  arrow: <><path d="M4 12h15" /><path d="m14 6 6 6-6 6" /></>,
  refresh: <><path d="M20 11a8 8 0 1 0-2 6.5" /><path d="M20 5v6h-6" /></>,
  trash: <><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13" /><path d="M10.5 11v5.5M13.5 11v5.5" /></>,
  dot: <circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5.5" /><circle cx="12" cy="8" r=".9" fill="currentColor" stroke="none" /></>,
  scan: <><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" /><path d="M4 12h16" /></>,
  search: <><circle cx="10.8" cy="10.8" r="6.4" /><path d="m15.6 15.6 4.4 4.4" /></>,
  bell: <><path d="M18 15.6V11a6 6 0 1 0-12 0v4.6L4.6 18h14.8L18 15.6Z" /><path d="M9.8 20.8a2.4 2.4 0 0 0 4.4 0" /></>,
  chevron: <path d="m6.5 9.75 5.5 5.5 5.5-5.5" />,
  folder: <><path d="M3.5 7.6a2 2 0 0 1 2-2h3.3l2 2.3h7.7a2 2 0 0 1 2 2v7.5a1.9 1.9 0 0 1-1.9 1.9H5.4a1.9 1.9 0 0 1-1.9-1.9V7.6Z" /><path d="M3.6 11.4h16.8" /></>,
  help: <><circle cx="12" cy="12" r="8.4" /><path d="M9.7 9.6a2.45 2.45 0 1 1 3.4 2.3c-.7.4-1.1.9-1.1 1.7v.3" /><circle cx="12" cy="16.9" r=".85" fill="currentColor" stroke="none" /></>,
  bulb: <><path d="M9.3 17.4h5.4" /><path d="M10.3 20.2h3.4" /><path d="M12 3.6a5.9 5.9 0 0 0-3.3 10.8v3h6.6v-3A5.9 5.9 0 0 0 12 3.6Z" /></>,
}

interface Props extends SVGProps<SVGSVGElement> {
  name: IconName
  size?: number
}

export function Icon({ name, size = 20, ...rest }: Props) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {paths[name]}
    </svg>
  )
}
