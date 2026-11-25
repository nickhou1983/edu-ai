targetScope = 'resourceGroup'

var settings = loadJsonContent('test.settings.json')
var tags = {
  application: 'edu-ai'
  environment: 'synthetic-test'
  'app-onboard-skill': 'true'
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: settings.network
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'private-endpoints'
  properties: {
    addressPrefix: '10.42.3.0/27'
    privateEndpointNetworkPolicies: 'Enabled'
  }
}

module blob 'modules/blob-private-link.bicep' = {
  name: 'edu-ai-blob-private-link'
  params: { tags: tags }
  dependsOn: [subnet]
}

output endpointId string = blob.outputs.endpointId
