import { describe, it, expect } from 'vitest'
import { pending } from './App.jsx'
describe('console helpers', () => {
  it('counts only incidents with a pending proposal', () => {
    expect(pending([{proposals:[{status:'pending'}]},{proposals:[{status:'approved'}]},{proposals:[]}])).toHaveLength(1)
  })
  it('handles missing proposals', () => { expect(pending([{}])).toHaveLength(0) })
})
