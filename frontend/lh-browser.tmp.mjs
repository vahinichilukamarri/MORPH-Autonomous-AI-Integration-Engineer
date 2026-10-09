import { chromium } from '@playwright/test'
const b = await chromium.launch({ args: ['--remote-debugging-port=9333'] })
console.log('ready')
await new Promise((r) => setTimeout(r, 15 * 60 * 1000))
await b.close()
