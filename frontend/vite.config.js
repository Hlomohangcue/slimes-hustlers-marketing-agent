import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export function validateProductionApiUrl(value) {
  if (!value) return 'VITE_API_BASE_URL is required for production builds.'
  try {
    const url = new URL(value)
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '')
    const isLoopback = hostname === 'localhost'
      || hostname.endsWith('.localhost')
      || hostname === '127.0.0.1'
      || hostname === '::1'
      || hostname === '::'
      || hostname === '0.0.0.0'
      || hostname.startsWith('::ffff:')
      || hostname.startsWith('127.')
    if (url.protocol !== 'https:') {
      return 'Production VITE_API_BASE_URL must use HTTPS.'
    }
    if (isLoopback) {
      return 'Production VITE_API_BASE_URL cannot point to localhost or loopback.'
    }
    if (url.username || url.password || url.search || url.hash) {
      return 'Production VITE_API_BASE_URL must not contain credentials, query, or fragment.'
    }
    return null
  } catch {
    return 'Production VITE_API_BASE_URL must be a valid absolute URL.'
  }
}

export function viteSecretVariableNames(envNames) {
  return envNames.filter((name) => (
    /^VITE_/i.test(name)
    && /AUTH|BEARER|TOKEN|SECRET|PASSWORD|CREDENTIAL|KEY/i.test(name)
  ))
}

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const apiBaseUrl = process.env.VITE_API_BASE_URL || env.VITE_API_BASE_URL
  const exposedSecrets = viteSecretVariableNames([
    ...new Set([...Object.keys(env), ...Object.keys(process.env)]),
  ])
  if (exposedSecrets.length) {
    throw new Error(`Secret-like Vite variables are forbidden: ${exposedSecrets.join(', ')}`)
  }
  if (command === 'build') {
    const validationError = validateProductionApiUrl(apiBaseUrl)
    if (validationError) throw new Error(validationError)
  }
  return {
    plugins: [react()],
  }
})
