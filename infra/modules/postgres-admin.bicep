param postgresName string
param principalName string
param principalId string

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' existing = {
  name: postgresName
}

resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2025-08-01' = {
  parent: postgres
  name: principalId
  properties: {
    principalName: principalName
    principalType: 'ServicePrincipal'
    tenantId: tenant().tenantId
  }
}
