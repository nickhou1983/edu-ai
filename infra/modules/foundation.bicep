param deployerObjectId string
param tags object

var settings = loadJsonContent('../test.settings.json')
var location = settings.location
var roles = {
  acrPull: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  blobWriter: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  blobReader: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  queueSender: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39')
  queueReceiver: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0')
  agentConsumer: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6')
  projectManager: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'eadc314b-1a2d-4efa-be10-5d325db5065e')
  foundryUser: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: settings.network
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.42.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            { name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
          serviceEndpoints: [
            { service: 'Microsoft.Storage' }
          ]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.42.2.0/27'
          delegations: [
            { name: 'postgres', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.42.3.0/27'
          privateEndpointNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

resource appsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'apps'
}

resource dbSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'postgres'
}

resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: settings.dbDns
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dns
  name: 'apps-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

var identityNames = [settings.apiIdentity, settings.workerIdentity, settings.migrationIdentity]
resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for name in identityNames: {
    name: name
    location: location
    tags: tags
  }
]

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: settings.postgres
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '18'
    storage: { storageSizeGB: 32, autoGrow: 'Disabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: {
      delegatedSubnetResourceId: dbSubnet.id
      privateDnsZoneArmResourceId: dns.id
      publicNetworkAccess: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
  }
  dependsOn: [dnsLink]
}

module postgresAdministrator 'postgres-admin.bicep' = {
  name: 'edu-ai-postgres-entra-admin'
  params: {
    postgresName: postgres.name
    principalName: settings.migrationIdentity
    principalId: identities[2].properties.principalId
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: settings.log
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    sku: { name: 'PerGB2018' }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: settings.insights
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: settings.environment
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: appsSubnet.id
      internal: false
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource appLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'app-logs'
  scope: environment
  properties: {
    workspaceId: workspace.id
    logs: [
      { category: 'ContainerAppConsoleLogs', enabled: true }
      { category: 'ContainerAppSystemLogs', enabled: true }
    ]
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: settings.registry
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    roleAssignmentMode: 'LegacyRegistryPermissions'
    policies: {
      azureADAuthenticationAsArmPolicy: { status: 'enabled' }
    }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: settings.storage
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      virtualNetworkRules: [
        { id: appsSubnet.id, action: 'Allow' }
      ]
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storage
  name: 'default'
}

resource worksheets 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobs
  name: settings.container
  properties: { publicAccess: 'None' }
}

module blobPrivateLink 'blob-private-link.bicep' = {
  name: 'edu-ai-blob-private-link'
  params: { tags: tags }
  dependsOn: [vnet, storage]
}

resource serviceBus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: settings.serviceBus
  location: location
  tags: tags
  sku: { name: 'Basic', tier: 'Basic' }
  properties: { disableLocalAuth: true, minimumTlsVersion: '1.2' }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: serviceBus
  name: settings.queue
  properties: {
    lockDuration: 'PT1M'
    maxDeliveryCount: 10
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P1D'
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: settings.foundry
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: settings.foundry
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: settings.model
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: settings.model, version: '2025-08-07' }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: settings.project
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: { displayName: settings.project, description: 'Synthetic math worksheet testing only' }
  dependsOn: [model]
}

resource registryConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'acr-connection'
  properties: {
    category: 'ContainerRegistry'
    target: registry.properties.loginServer
    authType: 'ManagedIdentity'
    isSharedToAll: true
    credentials: {
      clientId: project.identity!.principalId!
      resourceId: registry.id
    }
    metadata: { ResourceId: registry.id }
  }
}

resource workloadImagePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (_, index) in identityNames: {
    name: guid(registry.id, identities[index].id, roles.acrPull)
    scope: registry
    properties: {
      principalId: identities[index].properties.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: roles.acrPull
    }
  }
]

resource projectImagePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, project.id, roles.acrPull)
  scope: registry
  properties: {
    principalId: project.identity!.principalId!
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.acrPull
  }
}

resource projectModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, project.id, roles.foundryUser)
  scope: foundry
  properties: {
    principalId: project.identity!.principalId!
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.foundryUser
  }
}

resource apiBlobWrite 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(worksheets.id, identities[0].id, roles.blobWriter)
  scope: worksheets
  properties: {
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.blobWriter
  }
}

resource workerBlobRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(worksheets.id, identities[1].id, roles.blobReader)
  scope: worksheets
  properties: {
    principalId: identities[1].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.blobReader
  }
}

resource apiQueueSend 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(queue.id, identities[0].id, roles.queueSender)
  scope: queue
  properties: {
    principalId: identities[0].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.queueSender
  }
}

resource workerQueueReceive 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(queue.id, identities[1].id, roles.queueReceiver)
  scope: queue
  properties: {
    principalId: identities[1].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.queueReceiver
  }
}

resource workerAgentInvoke 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, identities[1].id, roles.agentConsumer)
  scope: project
  properties: {
    principalId: identities[1].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roles.agentConsumer
  }
}

resource projectRelease 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, deployerObjectId, roles.projectManager)
  scope: project
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: roles.projectManager
  }
}

output appOrigin string = 'https://${settings.api}.${environment.properties.defaultDomain}'
output projectEndpoint string = 'https://${settings.foundry}.services.ai.azure.com/api/projects/${settings.project}'
output registryLoginServer string = registry.properties.loginServer
