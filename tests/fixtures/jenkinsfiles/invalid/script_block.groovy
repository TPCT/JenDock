// expect: line 7: script blocks (free Groovy) are not supported; use sh steps
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                script {
                    echo 'x'
                }
            }
        }
    }
}
