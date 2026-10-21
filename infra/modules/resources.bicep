@minLength(1)
param location string
param tags object
param deployCompute bool = false
param containerImage string
param tenantId string
param apiAudience string
param corsOrigin string = ''
param cuEndpoint string = ''
param cuAnalyzerId string = 'campaign-video-v1'
param cuModelDeployments string = '{}'
param safetyEndpoint string = ''
param ocrEndpoint string = ''
param speechEndpoint string = ''
param speechLocales string = ''
param enablePaidAi bool = false
param uploadsRetentionDays int = 7
param reportsRetentionDays int = 30

param containerAppName string
param jobName string
param environmentResourceName string
param registryName string
param storageName string
param logWorkspaceName string
param identityName string

var appPort = 3000
var apiImage = containerImage
var resolvedCorsOrigin = empty(corsOrigin)
  ? (deployCompute ? 'https://${containerAppName}.${appEnvironment.properties.defaultDomain}' : '')
  : corsOrigin

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: 1
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: storageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    cors: {
      corsRules: empty(resolvedCorsOrigin)
        ? []
        : [
            {
              allowedOrigins: [
                resolvedCorsOrigin
              ]
              allowedMethods: [
                'OPTIONS'
                'PUT'
              ]
              allowedHeaders: [
                'content-type'
                'x-ms-blob-type'
                'if-none-match'
              ]
              exposedHeaders: [
                'ETag'
                'x-ms-request-id'
              ]
              maxAgeInSeconds: 600
            }
          ]
    }
  }
}

resource uploadsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: 'uploads'
  properties: {
    publicAccess: 'None'
  }
}

resource jobsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: 'jobs'
  properties: {
    publicAccess: 'None'
  }
}

resource reportsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: 'reports'
  properties: {
    publicAccess: 'None'
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-uploads'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['uploads/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: uploadsRetentionDays
                }
              }
            }
          }
        }
        {
          name: 'expire-reports-and-jobs'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['reports/', 'jobs/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: reportsRetentionDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2025-01-01' = {
  parent: storage
  name: 'default'
}

resource analysisQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01' = {
  parent: queueService
  name: 'analysis'
}

resource poisonQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01' = {
  parent: queueService
  name: 'analysis-poison'
}

resource appEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: environmentResourceName
  location: location
  tags: tags
  properties: {
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspace.properties.customerId
        sharedKey: logWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, managedIdentity.name, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource blobDelegator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, managedIdentity.name, 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, managedIdentity.name, '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, managedIdentity.name, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource api 'Microsoft.App/containerApps@2025-07-01' = if (deployCompute) {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    workloadProfileName: 'Consumption'
    managedEnvironmentId: appEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: appPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: managedIdentity.id
        }
      ]
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'MODE', value: 'azure' }
            { name: 'PORT', value: string(appPort) }
            { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
            { name: 'AZURE_CLIENT_ID', value: managedIdentity.properties.clientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'AZURE_API_AUDIENCE', value: apiAudience }
            { name: 'AZURE_REQUIRED_SCOPE', value: 'Video.Analyze' }
            { name: 'CORS_ORIGINS', value: corsOrigin }
            { name: 'ENABLE_PAID_AI', value: 'false' }
            { name: 'CU_ENDPOINT', value: cuEndpoint }
            { name: 'CU_ANALYZER_ID', value: cuAnalyzerId }
            { name: 'CU_MODEL_DEPLOYMENTS', value: cuModelDeployments }
            { name: 'SAFETY_ENDPOINT', value: safetyEndpoint }
            { name: 'OCR_ENDPOINT', value: ocrEndpoint }
            { name: 'MAX_UPLOAD_BYTES', value: '104857600' }
            { name: 'UPLOAD_TTL_SECONDS', value: '600' }
            { name: 'UPLOAD_WALL_SECONDS', value: '120' }
            { name: 'MAX_DURATION_SECONDS', value: '120' }
            { name: 'MAX_SAMPLE_FRAMES', value: '6' }
            { name: 'MEDIA_WALL_SECONDS', value: '45' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 15
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    blobDataContributor
    blobDelegator
    queueDataContributor
    acrPull
  ]
}

resource workerJob 'Microsoft.App/jobs@2025-07-01' = if (deployCompute) {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    workloadProfileName: 'Consumption'
    environmentId: appEnvironment.id
    configuration: {
      registries: [
        {
          server: registry.properties.loginServer
          identity: managedIdentity.id
        }
      ]
      replicaRetryLimit: 0
      replicaTimeout: 1200
      triggerType: 'Event'
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 1
          pollingInterval: 30
          rules: [
            {
              name: 'analysis-queue'
              type: 'azure-queue'
              metadata: {
                accountName: storage.name
                queueName: 'analysis'
                queueLength: '1'
              }
              identity: managedIdentity.id
            }
          ]
        }
      }
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: apiImage
          command: [
            'python'
            '-m'
            'videoextration.azure_worker'
          ]
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: [
            { name: 'MODE', value: 'azure' }
            { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
            { name: 'AZURE_CLIENT_ID', value: managedIdentity.properties.clientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'AZURE_REQUIRED_SCOPE', value: 'Video.Analyze' }
            { name: 'ENABLE_PAID_AI', value: enablePaidAi ? 'true' : 'false' }
            { name: 'SPEECH_ENDPOINT', value: speechEndpoint }
            { name: 'SPEECH_LOCALES', value: speechLocales }
            { name: 'CU_ENDPOINT', value: cuEndpoint }
            { name: 'CU_ANALYZER_ID', value: cuAnalyzerId }
            { name: 'CU_MODEL_DEPLOYMENTS', value: cuModelDeployments }
            { name: 'SAFETY_ENDPOINT', value: safetyEndpoint }
            { name: 'OCR_ENDPOINT', value: ocrEndpoint }
            { name: 'MAX_UPLOAD_BYTES', value: '104857600' }
            { name: 'MAX_DURATION_SECONDS', value: '120' }
            { name: 'MAX_SAMPLE_FRAMES', value: '6' }
            { name: 'MEDIA_WALL_SECONDS', value: '45' }
          ]
        }
      ]
    }
  }
  dependsOn: [
    blobDataContributor
    blobDelegator
    queueDataContributor
    acrPull
    analysisQueue
    poisonQueue
  ]
}

output managedIdentityPrincipalId string = managedIdentity.properties.principalId
output apiOrigin string = deployCompute ? 'https://${api!.properties.configuration!.ingress!.fqdn}' : ''
