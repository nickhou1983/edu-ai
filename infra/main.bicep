targetScope = 'subscription'

@minLength(36)
@maxLength(36)
param deployerObjectId string

var settings = loadJsonContent('test.settings.json')
var tags = {
  application: 'edu-ai'
  environment: 'synthetic-test'
  'app-onboard-skill': 'true'
}

resource group 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: settings.resourceGroup
  location: settings.location
  tags: tags
}

module foundation 'modules/foundation.bicep' = {
  name: 'edu-ai-foundation'
  scope: group
  params: {
    deployerObjectId: deployerObjectId
    tags: tags
  }
}

output appOrigin string = foundation.outputs.appOrigin
output projectEndpoint string = foundation.outputs.projectEndpoint
output registryLoginServer string = foundation.outputs.registryLoginServer
