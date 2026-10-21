targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string

@minLength(1)
param location string

param sessionId string
param deployedBy string
param createdAt string

@description('Deploy the API and event-driven worker only after the real image has been built and pushed to ACR.')
param deployCompute bool = false

@description('Real image to deploy when deployCompute is true. Do not use a placeholder image.')
param imageTag string = 'latest'
param containerImage string = ''

@description('Non-empty Entra tenant ID for compute deployment.')
@minLength(36)
param tenantId string = '16b3c013-d300-468d-ac64-7eda0820b6d3'

@description('Non-empty API audience/client identifier for compute deployment.')
@minLength(1)
param apiAudience string

@description('Exact browser origin allowed by the API. Leave empty when no browser origin is configured.')
param corsOrigin string = ''

@description('Existing Content Understanding endpoint. Paid AI remains disabled by default.')
param cuEndpoint string = ''
param cuAnalyzerId string = 'campaign-video-v1'
param cuModelDeployments string = '{}'
param safetyEndpoint string = ''
param ocrEndpoint string = ''
param speechEndpoint string = ''
param speechLocales string = ''
param enablePaidAi bool = false
@minValue(1)
param uploadsRetentionDays int = 7
@minValue(1)
param reportsRetentionDays int = 30

@description('Optional override for the deployment resource names from prepare-plan.json.')
param resourceGroupName string = 'rg-videoextra-dev-1737'
param containerAppName string = 'ca-videoextra-dev-1737'
param jobName string = 'job-videoextra-dev-1737'
param environmentResourceName string = 'env-videoextra-dev-1737'
param registryName string = 'crvideoextradev1737'
param storageName string = 'stvideoextradev1737'
param logWorkspaceName string = 'log-videoextra-dev-1737'
param identityName string = 'id-videoextra-dev-1737'

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources './modules/resources.bicep' = {
  name: 'videoextra-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    deployCompute: deployCompute
    containerImage: containerImage == '' ? '${registryName}.azurecr.io/videoextraction:${imageTag}' : containerImage
    tenantId: tenantId
    apiAudience: apiAudience
    corsOrigin: corsOrigin
    cuEndpoint: cuEndpoint
    cuAnalyzerId: cuAnalyzerId
    cuModelDeployments: cuModelDeployments
    safetyEndpoint: safetyEndpoint
    ocrEndpoint: ocrEndpoint
    speechEndpoint: speechEndpoint
    speechLocales: speechLocales
    enablePaidAi: enablePaidAi
    uploadsRetentionDays: uploadsRetentionDays
    reportsRetentionDays: reportsRetentionDays
    containerAppName: containerAppName
    jobName: jobName
    environmentResourceName: environmentResourceName
    registryName: registryName
    storageName: storageName
    logWorkspaceName: logWorkspaceName
    identityName: identityName
  }
}

output deploymentSubscription string = subscription().subscriptionId
output storageAccountName string = storageName
output managedIdentityPrincipalId string = resources.outputs.managedIdentityPrincipalId
output apiOrigin string = resources.outputs.apiOrigin
