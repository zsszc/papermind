const crypto = require('node:crypto')
const net = require('node:net')


function getAvailablePort({ createServer = net.createServer } = {}) {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen({ host: '127.0.0.1', port: 0, exclusive: true }, () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : null
      server.close((error) => {
        if (error) reject(error)
        else if (!Number.isInteger(port) || port < 1024 || port > 65535) {
          reject(new Error('无法获取非特权随机回环端口'))
        }
        else resolve(port)
      })
    })
  })
}


async function createRuntimeIdentity({
  getPort = getAvailablePort,
  randomBytes = crypto.randomBytes,
  randomUUID = crypto.randomUUID,
} = {}) {
  return {
    port: await getPort(),
    token: randomBytes(32).toString('hex'),
    instanceId: randomUUID(),
  }
}


function isAllowedRuntimeConfigRequest(event, trustedWebContents, productionEntryUrl) {
  if (!event?.sender || event.sender !== trustedWebContents) return false
  try {
    const candidate = new URL(event.sender.getURL())
    const entry = new URL(productionEntryUrl)
    candidate.hash = ''
    candidate.search = ''
    entry.hash = ''
    entry.search = ''
    return candidate.protocol === 'file:' && candidate.href === entry.href
  } catch {
    return false
  }
}


module.exports = {
  createRuntimeIdentity,
  getAvailablePort,
  isAllowedRuntimeConfigRequest,
}
