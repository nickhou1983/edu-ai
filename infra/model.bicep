targetScope = 'resourceGroup'

var settings = loadJsonContent('test.settings.json')
var selected = settings.agentModel

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: settings.foundry
}

resource agentModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: selected.deployment
  sku: { name: selected.sku, capacity: selected.capacity }
  properties: {
    model: { format: 'OpenAI', name: selected.name, version: selected.version }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output modelDeployment string = agentModel.name
