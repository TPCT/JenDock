pipeline {
    agent { docker 'alpine:3.20' }
    stages {
        stage('Work') {
            steps {
                sh 'make'
            }
            post {
                success { echo 'stage ok' }
                always { echo 'stage done' }
            }
        }
    }
    post {
        cleanup { sh 'rm -rf build' }
        unstable { echo 'unstable' }
        aborted { echo 'aborted' }
        changed { echo 'changed' }
        failure { error 'build failed' }
        always { echo 'finished' }
        success { echo 'ok' }
    }
}
