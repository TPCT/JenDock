// expect: line 7: 'returnStdout' is not supported
pipeline {
    agent { docker 'alpine:3.20' }
    stages {
        stage('A') {
            steps {
                sh(script: 'date', returnStdout: true)
            }
        }
    }
}
