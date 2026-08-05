import { useCallback, useEffect, useRef, useState } from 'react'

/** Run an async loader on mount (and whenever deps change), with reload support. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const alive = useRef(true)

  const run = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await loader()
      if (alive.current) setData(result)
    } catch (err) {
      if (alive.current) setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (alive.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    alive.current = true
    void run()
    return () => {
      alive.current = false
    }
  }, [run])

  return { data, loading, error, reload: run, setData }
}

/** Delay a fast-changing value — used for search-as-you-type. */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}
