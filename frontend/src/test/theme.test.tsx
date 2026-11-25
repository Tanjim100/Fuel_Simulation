/**
 * The theme toggle.
 *
 * The console ships two themes. The preference is persisted, because an
 * operator who works a night shift should not have to re-pick it every
 * morning, and it falls back to the operating system's preference when
 * nothing has been chosen.
 *
 * The switch is a single attribute on the document element — the CSS keys
 * off it, so no component re-renders with new colours and no colour logic
 * lives in JavaScript.
 */

import { describe, expect, it, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StatusBar } from '../components/StatusBar'
import { useTheme } from '../lib/useTheme'
import { broker, metrics, routes, stations } from './fixtures'

const STORAGE_KEY = 'fuel-console-theme'

function ThemeProbe() {
  const { theme, toggle } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggle}>toggle</button>
    </div>
  )
}

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  vi.restoreAllMocks()
})

describe('the theme toggle', () => {
  it('defaults to dark when nothing is stored and the OS expresses no preference', () => {
    render(<ThemeProbe />)
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
  })

  it('writes the theme onto the document element so the CSS can key off it', async () => {
    render(<ThemeProbe />)
    await waitForAttribute('dark')

    await userEvent.click(screen.getByText('toggle'))
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('persists the choice so it survives a reload', async () => {
    render(<ThemeProbe />)
    await userEvent.click(screen.getByText('toggle'))
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('light')
  })

  it('restores a stored preference on the next visit', () => {
    window.localStorage.setItem(STORAGE_KEY, 'light')
    render(<ThemeProbe />)
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
  })

  it('follows the operating system when nothing has been stored', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({ matches: true }),
    )
    render(<ThemeProbe />)
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    vi.unstubAllGlobals()
  })

  it('does not crash when the environment has no matchMedia', () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(() => render(<ThemeProbe />)).not.toThrow()
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    vi.unstubAllGlobals()
  })

  it('does not crash when localStorage is unavailable', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    expect(() => render(<ThemeProbe />)).not.toThrow()
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    getItem.mockRestore()
  })

  it('offers the toggle in the status bar', () => {
    render(
      <StatusBar
        health={null}
        broker={broker}
        metrics={metrics}
        stations={stations}
        routes={routes}
        theme="dark"
        onToggleTheme={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: /switch to light theme/i })).toBeInTheDocument()
  })
})

/** Wait for the effect that writes the attribute to have run. */
async function waitForAttribute(expected: string) {
  const { waitFor } = await import('@testing-library/react')
  await waitFor(() => {
    expect(document.documentElement.dataset.theme).toBe(expected)
  })
}
