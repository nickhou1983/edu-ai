param tags object

var settings = loadJsonContent('../test.settings.json')

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: settings.network
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'private-endpoints'
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: settings.storage
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${az.environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'apps-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: settings.blobPrivateEndpoint
  location: settings.location
  tags: tags
  properties: {
    subnet: { id: subnet.id }
    privateLinkServiceConnections: [
      {
        name: 'worksheets-blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'blob', properties: { privateDnsZoneId: zone.id } }
    ]
  }
  dependsOn: [link]
}

output endpointId string = endpoint.id
