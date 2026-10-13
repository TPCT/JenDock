// expect: line 7: sh runs inside a container
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                sh 'make'
            }
        }
    }
}
