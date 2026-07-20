import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { colors } from '../theme'

const DIVIDER_HEIGHT = 6

function clamp(num, min, max) {
  return Math.max(min, Math.min(max, num))
}

export default function ResizableVertical({
  storageKey,
  top,
  bottom,
  minTop = 120,
  minBottom = 90,
  defaultTopRatio = 0.72,
  style,
  className,
}) {
  const containerRef = useRef(null)
  const [tick, setTick] = useState(0)
  const [dragging, setDragging] = useState(false)

  const [topHeight, setTopHeight] = useState(() => {
    if (!storageKey) return null
    try {
      const raw = localStorage.getItem(`ResizableVertical:${storageKey}`)
      if (raw) return JSON.parse(raw)
    } catch (e) {
      // ignore
    }
    return null
  })

  useEffect(() => {
    if (!storageKey) return
    localStorage.setItem(
      `ResizableVertical:${storageKey}`,
      JSON.stringify(topHeight)
    )
  }, [topHeight, storageKey])

  useEffect(() => {
    if (!containerRef.current) return
    if (typeof ResizeObserver === 'undefined') {
      const onResize = () => setTick((t) => t + 1)
      window.addEventListener('resize', onResize)
      return () => window.removeEventListener('resize', onResize)
    }
    const ro = new ResizeObserver(() => setTick((t) => t + 1))
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [])

  const containerHeight = containerRef.current?.clientHeight || 0

  useEffect(() => {
    if (!containerHeight) return
    const maxTop = containerHeight - DIVIDER_HEIGHT - minBottom
    setTopHeight((h) => {
      if (h == null) return clamp(containerHeight * defaultTopRatio, minTop, maxTop)
      return clamp(h, minTop, maxTop)
    })
  }, [containerHeight, defaultTopRatio, minBottom, minTop])

  const handleMouseDown = useCallback(
    (e) => {
      e.preventDefault()
      setDragging(true)
      const startY = e.clientY
      const startTop = topHeight

      const onMove = (ev) => {
        const delta = ev.clientY - startY
        const maxTop = containerHeight - DIVIDER_HEIGHT - minBottom
        setTopHeight(clamp(startTop + delta, minTop, maxTop))
      }

      const onUp = () => {
        setDragging(false)
        document.removeEventListener('mousemove', onMove)
        document.removeEventListener('mouseup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }

      document.body.style.cursor = 'row-resize'
      document.body.style.userSelect = 'none'
      document.addEventListener('mousemove', onMove)
      document.addEventListener('mouseup', onUp)
    },
    [containerHeight, minBottom, minTop, topHeight]
  )

  const bottomHeight = Math.max(0, containerHeight - (topHeight || minTop) - DIVIDER_HEIGHT)

  return (
    <div
      ref={containerRef}
      className={className}
      style={{
        display: 'flex',
        flexDirection: 'column',
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        ...style,
      }}
    >
      <div style={{ height: topHeight || minTop, minHeight: minTop, overflow: 'hidden' }}>
        {top}
      </div>

      <div
        onMouseDown={handleMouseDown}
        style={{
          height: DIVIDER_HEIGHT,
          flexShrink: 0,
          cursor: 'row-resize',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'transparent',
        }}
      >
        <div
          style={{
            width: 36,
            height: 2,
            borderRadius: 2,
            background: dragging ? colors.primary : colors.border,
            transition: 'background 0.2s',
          }}
        />
      </div>

      <div
        style={{
          height: bottomHeight,
          minHeight: minBottom,
          overflow: 'hidden',
        }}
      >
        {bottom}
      </div>
    </div>
  )
}
