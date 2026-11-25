/**
 * The console's theme preference.
 *
 * Two themes, one set of token *names*. The CSS keys off
 * `document.documentElement.dataset.theme`, so switching is a single
 * attribute write — no component re-renders with new colours, and no
 * colour logic lives in JavaScript.
 *
 * The preference is persisted, because an operator who works a night shift
 * should not have to re-pick the theme every morning. When nothing is
 * stored we follow the operating system's preference, and fall back to dark.
 *
 * Neither `localStorage` nor `matchMedia` is guaranteed to exist — a bare
 * test environment, or a browser with storage disabled — so both are guarded.
 * The console must render even when it cannot remember.
 */

import { useCallback, useEffect, useState } from 'react'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'fuel-console-theme'

/** The theme to use when nothing is stored and the OS expresses no preference. */
const DEFAULT_THEME: Theme = 'dark'

function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light'
}

/**
 * The theme the operating system asks for.
 *
 * `matchMedia` is absent in some environments; when it is, we have no
 * opinion and the caller falls back to the default.
 */
function systemTheme(): Theme | null {
  if (typeof window === 'undefined') return null
  if (typeof window.matchMedia !== 'function') return null
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  } catch {
    return null
  }
}

/** The stored preference, if there is a readable one. */
function storedTheme(): Theme | null {
  if (typeof window === 'undefined') return null
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return isTheme(value) ? value : null
  } catch {
    // Storage can throw when it is disabled or full. A theme preference is
    // not worth failing a render over.
    return null
  }
}

function persistTheme(theme: Theme): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    /* storage unavailable — the theme still applies for this session */
  }
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() => storedTheme() ?? systemTheme() ?? DEFAULT_THEME)

  // The attribute is the source of truth for the CSS. Setting it on the
  // document element means the whole console flips in one paint.
  useEffect(() => {
    if (typeof document === 'undefined') return
    document.documentElement.dataset.theme = theme
  }, [theme])

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      persistTheme(next)
      return next
    })
  }, [])

  return { theme, toggle }
}
