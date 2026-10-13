pipeline {
    agent {
        docker {
            image 'node:22'
        }
    }
    environment {
        NEXT_TELEMETRY_DISABLED = '1'
        NPM_TOKEN = credentials('NPM_TOKEN')
    }
    stages {
        stage('Install') {
            steps {
                sh 'npm ci'
            }
        }
        stage('Lint and test') {
            parallel {
                stage('Lint') {
                    steps {
                        sh 'npm run lint'
                    }
                }
                stage('Unit tests') {
                    steps {
                        sh(script: 'npm test', label: 'jest')
                    }
                }
            }
        }
        stage('Build') {
            steps {
                sh 'npm run build'
                dockerBuild image: 'next-web', args: [NODE_ENV: 'production', API_URL: "https://${ENVIRONMENT}.example.test"]
            }
        }
        stage('Release') {
            when {
                allOf {
                    branch 'main'
                    not { environment name: 'ENVIRONMENT', value: 'staging' }
                }
            }
            steps {
                deploy(services: [web: 'next-web'])
                healthCheck url: 'http://web.internal/health'
            }
        }
    }
}
