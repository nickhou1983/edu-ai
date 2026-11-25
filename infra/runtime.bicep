targetScope = 'resourceGroup'

@allowed(['migrate', 'worker', 'api', 'serve'])
param phase string

@minLength(71)
@maxLength(71)
param imageDigest string

param agentUrl string

@minLength(36)
@maxLength(36)
param apiClientId string

@minLength(36)
@maxLength(36)
param spaClientId string

var settings = loadJsonContent('test.settings.json')
var names = [settings.apiIdentity, settings.workerIdentity, settings.migrationIdentity]
var tags = {
  application: 'edu-ai'
  environment: 'synthetic-test'
  'app-onboard-skill': 'true'
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: settings.environment
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: settings.registry
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' existing = {
  name: settings.postgres
}

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [
  for name in names: { name: name }
]

var loginServer = registry.properties.loginServer
var image = '${loginServer}/edu-app@${imageDigest}'
var dbEndpoint = '${postgres.properties.fullyQualifiedDomainName}/${settings.database}'
var profiles = [
  for (_, index) in names: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identities[index].id}': {}
    }
  }
]
var commonEnv = [
  { name: 'EDU_MODE', value: 'azure' }
  { name: 'EDU_AGENT_URL', value: agentUrl }
  { name: 'EDU_BLOB_ACCOUNT_URL', value: 'https://${settings.storage}.blob.${az.environment().suffixes.storage}' }
  { name: 'EDU_BLOB_CONTAINER', value: settings.container }
  { name: 'EDU_SERVICE_BUS_NAMESPACE', value: '${settings.serviceBus}.servicebus.windows.net' }
  { name: 'EDU_SERVICE_BUS_QUEUE', value: settings.queue }
  { name: 'EDU_ENTRA_TENANT_ID', value: tenant().tenantId }
  { name: 'EDU_ENTRA_API_CLIENT_ID', value: apiClientId }
  { name: 'EDU_ENTRA_SPA_CLIENT_ID', value: spaClientId }
]
func workloadEnv(common object[], role string, db string, clientId string) object[] => concat(common, [
  { name: 'EDU_DATABASE_URL', value: 'postgresql+psycopg://${role}@${db}' }
  { name: 'EDU_MANAGED_IDENTITY_CLIENT_ID', value: clientId }
])

var resources = { cpu: json('0.5'), memory: '1Gi' }

resource migration 'Microsoft.App/jobs@2025-07-01' = if (phase == 'migrate') {
  name: settings.migration
  location: settings.location
  tags: tags
  identity: profiles[2]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: loginServer, identity: identities[2].id }]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          command: ['python', '/app/bootstrap_db.py']
          env: concat(workloadEnv(commonEnv, names[2], dbEndpoint, identities[2].properties.clientId), [
            { name: 'EDU_DB_API_ROLE', value: settings.apiIdentity }
            { name: 'EDU_DB_WORKER_ROLE', value: settings.workerIdentity }
            { name: 'EDU_DB_API_OBJECT_ID', value: identities[0].properties.principalId }
            { name: 'EDU_DB_WORKER_OBJECT_ID', value: identities[1].properties.principalId }
          ])
          resources: resources
        }
      ]
    }
  }
}

resource api 'Microsoft.App/containerApps@2025-07-01' = if (contains(['api', 'serve'], phase)) {
  name: settings.api
  location: settings.location
  tags: tags
  identity: profiles[0]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [{ server: loginServer, identity: identities[0].id }]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          command: ['python', '-m', 'edu_app', 'serve']
          env: workloadEnv(commonEnv, names[0], dbEndpoint, identities[0].properties.clientId)
          resources: resources
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 8000 }
              periodSeconds: 10
              timeoutSeconds: 8
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource worker 'Microsoft.App/jobs@2025-07-01' = if (contains(['worker', 'serve'], phase)) {
  name: settings.worker
  location: settings.location
  tags: tags
  identity: profiles[1]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Event'
      replicaTimeout: 900
      replicaRetryLimit: 0
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 2
          pollingInterval: 30
          rules: [
            {
              name: 'pending-jobs'
              type: 'azure-servicebus'
              identity: identities[1].id
              metadata: {
                namespace: settings.serviceBus
                queueName: settings.queue
                messageCount: '1'
              }
            }
          ]
        }
      }
      registries: [{ server: loginServer, identity: identities[1].id }]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: image
          command: ['python', '-m', 'edu_app', 'consume']
          env: workloadEnv(commonEnv, names[1], dbEndpoint, identities[1].properties.clientId)
          resources: resources
        }
      ]
    }
  }
}
